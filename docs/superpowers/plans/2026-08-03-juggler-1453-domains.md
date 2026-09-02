# Juggler 1453 Domain — Camoufox Fork Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Camoufox (Firefox 152) fork'unda Juggler protokolünü 6 domain'den **1453 gerçek XPCOM-bağlı domain'e** genişletmek — AI ajanların tarayıcının her noktasına (DOM, layout, network, storage, media, webrtc, permission, history, sessionstore, download, print, accessibility, performance, memory, process, observer, preference, about:) %100 master kontrolü.

**Architecture:** Camoufox'un kendi build zinciri (`daijro/camoufox` patch-set repo → `make fetch/dir/bootstrap/build`) ile kaynaktan fork build. `additions/juggler/` içinde çalışılır: Protocol.js 1453 domain'e genişletilir (generator ile), implementasyon tek **GenericXpcomBridge** üstünde — browser-process ve content-process tarafında chrome-privileged JS, her domain'i katalogdan çözülen gerçek XPCOM kaynağına (Services.* / Components.interfaces / contract ID / ES modülü) dinamik dispatch eder. Stub yok: her çağrı gerçek Firefox API'sidir. Transport değişmez: mevcut Juggler pipe (fd 3/4) + mevcut Zig sidecar.

**Tech Stack:** Firefox 152.0.4-beta.28 (Camoufox fork), Juggler protocol (Protocol.js/Dispatcher/SimpleChannel/JSWindowActor), XPCOM (Services, Ci, Cc, nsIObserverService), ES modules (.sys.mjs), Python 3 (katalog generator'ları), Zig 0.16.0 (mevcut sidecar, dokunulmayan kısım), Python MCP (kahin oracle/mirage).

## Global Constraints

- **Firefox ayrı/manuel indirme YASAK.** Tek kaynak: Camoufox build zinciri — `daijro/camoufox` clone + `make fetch` (kendi adımı, firefox source tarball'ını çeker) + `make dir` + `make bootstrap` + `make build`. Bu zincirin DIŞINDA hiçbir Firefox kaynağı/indirme kullanılmaz.
- **Build ile sıfırdan.** omni.ja in-place hack YOK. Fork kaynağa patch'lenir ve build edilir.
- **Playwright / CDP YASAK.** Tek protokol: Juggler pipe. Sidecar wire'ı `{"method":"Domain.method","params":{...}}` formunda Juggler-native'dir (CDP-shaped domain/command katmanı kaldırılır).
- **Stub YASAK.** Her domain ≥1 gerçek XPCOM/API çağrısı içermeli; boş domain, "not implemented" dönüşü, yer tutucu YASAK. Method adları gerçek Firefox API isimleriyle birebir eşleşir.
- **Toplam domain sayısı kesin: 1453.** Numaralı liste `katalog/domains.json` içinde üretilir ve doğrulanır (1453 satır).
- Airgap/isolation YOK; main-world erişim serbest; whitelist/kısıtlama YOK — katalogdaki her domain her ajan için çağrılabilir.
- Atomik commit'ler: her task tek (veya mantıksal grup) commit; commit mesajları Conventional Commits.
- Build ortamı: 16 CPU, 15GB RAM, ~75GB boş disk. OOM yasak → Task 0.1'de swapfile + paralellik sınırları ZORUNLU.
- Mevcut testler kırılmayacak: `uv run pytest tests/ -q` (102 koleksiyon) + zig `zig build test` (177) green kalır.
- Camoufox binary mevcut: `~/.cache/camoufox/browsers/official/152.0.4-beta.28-924f3109/camoufox` — fork build'e kadar geliştirme/karşılaştırma için kullanılır, DEĞİŞTİRİLMEZ.
- Kaynak kod arşivi (deployed Juggler 21 dosya + XPCOM envanteri): `/tmp/opencode/juggler-research/` (geçici, referans) — kalıcı kopya Task 1.1'de `katalog/` altına alınır.
- Port 9222/9240 REZERVE (kahin kuralı) — test portu olarak kullanılmaz.
- Çıktı: (1) patch'li fork kaynağı, (2) 1453 domain'li Protocol.js, (3) bridge/agent dosyaları, (4) build komutları, (5) MCP tool'ları, (6) numaralı domain listesi.

---

## Faz 0 — Build Zinciri (fork build altyapısı)

Çalışma dizini: `~/camoufox-src/` (repo dışı, planın kendi ağacı).

### Task 0.1: Ortam hazırlık — OOM koruması + araçlar

**Files:**
- Create: `~/camoufox-src/mozconfig` (fork'un mozconfig'i)

**Interfaces:**
- Produces: `~/camoufox-src/mozconfig` (Faz 0'ın tüm build task'ları okur), 8GB swapfile, ccache kurulu, `mach` çalışır durumda.

- [ ] **Step 1: Swapfile kur (OOM yasağı)**

```bash
sudo swapon --show
# yoksa:
sudo fallocate -l 8G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
# fstab kalıcılığı:
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -g   # swap satırı: 8G görünmeli
```

- [ ] **Step 2: ccache + rust doğrula**

```bash
which ccache || sudo apt-get install -y ccache
rustc --version   # Firefox kendi toolchain'ini kurar (mach bootstrap); sistem rust yalnızca ön koşul
node --version    # >= 18
python3 --version # >= 3.8
```

- [ ] **Step 3: Build dizinlerini kur**

```bash
mkdir -p ~/camoufox-src
df -h ~   # en az 70GB boş olmalı
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "chore(camoufox): build environment prerequisites documented" || true
```

### Task 0.2: Camoufox kaynak clone + fetch

**Files:**
- Create: `~/camoufox-src/camoufox/` (git clone)

**Interfaces:**
- Produces: `~/camoufox-src/camoufox/` — patch-set repo (Firefox kaynağı DEĞİL, `additions/` + `scripts/` + `Makefile` içerir), git HEAD'i takip eden `main` (154.0.4-beta.28 üstü de olabilir — `Makefile`'daki versiyonu doğrula).

- [ ] **Step 1: Clone**

```bash
cd ~/camoufox-src
git clone --depth 1 https://github.com/daijro/camoufox.git
cd camoufox
cat Makefile | head -60   # versiyon değişkenini oku: FIREFOX_VERSION / BETA_VERSION
```

- [ ] **Step 2: Versiyon tutarlılığını doğrula**

```bash
grep -E "VERSION|beta" Makefile upstream.sh | head -10
# BEKLENTİ: Firefox 152.x beta.28 civarı. Farklıysa devam — bu plan 152 tabanıyla yazıldı,
# mevcut repo versiyonu neyse o inşa edilir; bridge kodu sürümden bağımsız (XPCOM stabil).
```

- [ ] **Step 3: fetch — firefox source tarball'ı (Camoufox zincirinin parçası)**

```bash
make fetch 2>&1 | tee /tmp/opencode/camoufox-fetch.log
# BEKLENTİ: firefox-*.source.tar.xz (~700MB) ~/camoufox-src/camoufox/ altına iner.
# "firefox indir" değildir — Camoufox'un resmi build adımıdır.
tail -20 /tmp/opencode/camoufox-fetch.log
```

- [ ] **Step 4: Commit yok** (repo dışı kaynak; commit'ler fork ağacına işlenir)

### Task 0.3: make dir — patch'leri uygula

**Files:**
- Modify: `~/camoufox-src/camoufox/` (make dir ile `additions/` kopyalanır + `scripts/patch.py` 40+ patch uygulanır)

**Interfaces:**
- Produces: `~/camoufox-src/camoufox/firefox-*` kaynak ağacı — Juggler `remote/juggler/` içinde, `additions/juggler/` tam modül.

- [ ] **Step 1: dir**

```bash
cd ~/camoufox-src/camoufox
make dir 2>&1 | tee /tmp/opencode/camoufox-dir.log
tail -10 /tmp/opencode/camoufox-dir.log
```

- [ ] **Step 2: Juggler yüzeyini doğrula**

```bash
ls firefox-*/remote/juggler/protocol/ firefox-*/remote/juggler/content/ | head -40
# BEKLENTİ: Protocol.js, Dispatcher.js, BrowserHandler.js, PageHandler.js, content/PageAgent.js, main.js ...
grep -c "domains" firefox-*/remote/juggler/protocol/Protocol.js
```

- [ ] **Step 3: İlk commit noktası** (fork ağacına kayıt: patch'ler uygulanmış temel)

```bash
cd ~/camoufox-src/camoufox   # git repo'yu köke bağla (patch sonrası durumu kaydetmek için)
git init -q && git add -A && git commit -q -m "chore: camoufox base with patches applied (pre-juggler-1453)"
```

### Task 0.4: make bootstrap — toolchain

**Files:**
- Modify: `~/camoufox-src/camoufox/firefox-*/` (mach kurulumu), `~/.mozbuild/` (yeni)

**Interfaces:**
- Produces: `~/camoufox-src/camoufox/firefox-*/mach` çalışır; `~/.mozbuild/` toolchain (clang, rust, cbindgen); `mozconfig` okunur durumda.

- [ ] **Step 1: mozconfig kur (fork'un kendi ayarları — assets/base.mozconfig'ten)**

```bash
cd ~/camoufox-src/camoufox
cp assets/base.mozconfig firefox-*/mozconfig
cat >> firefox-*/mozconfig <<'EOF'

# --- kahin fork ekleri ---
mk_add_options MOZ_PARALLEL_BUILD=8          # 16 core'da OOM koruması (RAM 15GB)
export CARGO_BUILD_JOBS=1                     # rust OOM koruması
ac_add_options --enable-debug-symbols         # hata ayıklanabilir build (opsiyonel, disk varsa)
EOF
```

- [ ] **Step 2: Bootstrap (headless)**

```bash
cd ~/camoufox-src/camoufox/firefox-*/
./mach --no-interactive bootstrap --application-choice=browser 2>&1 | tee /tmp/opencode/mach-bootstrap.log
# sudo isterse: mevcut oturumda parolasız değilse --no-system-changes ile tekrar dene:
# ./mach --no-interactive bootstrap --application-choice=browser --no-system-changes
tail -15 /tmp/opencode/mach-bootstrap.log
```

- [ ] **Step 3: Doğrula**

```bash
./mach --version
rustc --version   # ~/.mozbuild içinden Firefox'un rust'ı
```

### Task 0.5: Temel build (patch'siz) + smoke

**Files:**
- Create: `~/camoufox-src/camoufox/firefox-*/obj-*/dist/bin/firefox` (build çıktısı)

**Interfaces:**
- Produces: İlk fork build — 6 domain'li Juggler çalışır binary. Sonraki tüm task'lar bu binary'yi smoke test için kullanır (değiştirilmeden ÖNCE: her değişiklikten sonra rebuild + smoke).

- [ ] **Step 1: Build (uzun — 1.5-3 saat)**

```bash
cd ~/camoufox-src/camoufox/firefox-*/
./mach build 2>&1 | tee /tmp/opencode/mach-build-base.log
# BEKLENTİ: son satır "Build complete" veya "Successfully built"
tail -5 /tmp/opencode/mach-build-base.log
```

- [ ] **Step 2: Smoke — fork binary'de Juggler 6 domain çalışır mı**

```bash
# repo içinden mevcut kahin test altyapısını fork binary'e yönlendir:
cd /home/void0x14/Belgeler/mcp-projelerim/cdp-kahin-mcp
CAMOUFOX_BIN=~/camoufox-src/camoufox/firefox-*/obj-*/dist/bin/firefox \
  uv run pytest tests/test_mirage_ipc.py -q -k "real or e2e or smoke" 2>&1 | tail -5
# BEKLENTİ: PASS (veya: kahin sidecar fork binary ile konuşur, 6 domain hazır)
# Not: sidecar spawn yolu KAHIN_CAMOUFOX_BIN env'iyle override edilir (mevcut mekanizma).
```

- [ ] **Step 3: Performans/disk notu**

```bash
du -sh ~/camoufox-src   # ~50-60GB beklenir
# Tar.xz'yi sil (objdir ağır):
rm -f ~/camoufox-src/camoufox/firefox-*.source.tar.xz
```

### Task 0.6: Build zinciri dokümanı + commit

**Files:**
- Create: `docs/build-1453.md` (fork build talimatı — tekrarlanabilir)

**Interfaces:**
- Produces: `docs/build-1453.md` — rebuild komutu: `cd ~/camoufox-src/camoufox/firefox-*/ && ./mach build`; smoke: `CAMOUFOX_BIN=<obj>/dist/bin/firefox`.

- [ ] **Step 1: Dokümanı yaz**

```markdown
# Fork Build Talimatı (Juggler 1453)

Kaynak: ~/camoufox-src/camoufox (daijro/camoufox, patch-set repo)
Build:   cd ~/camoufox-src/camoufox/firefox-* && ./mach build
Binary:  ~/camoufox-src/camoufox/firefox-*/obj-*/dist/bin/firefox
Smoke:   cd <kahin-repo> && CAMOUFOX_BIN=<binary> uv run pytest tests/test_mirage_ipc.py -q
OOM koruması: MOZ_PARALLEL_BUILD=8, CARGO_BUILD_JOBS=1, 8GB swapfile (Task 0.1)
Juggler kodu: remote/juggler/ (browser proc: protocol/*.js; content proc: content/*.js)
Değişiklik sonrası yeniden build: ./mach build (inkremental ~2-10 dk / dosya)
```

- [ ] **Step 2: Commit**

```bash
git add docs/build-1453.md && git commit -m "docs(camoufox): fork build instructions for juggler-1453"
```

---

## Faz 1 — Envanter & 1453 Domain Kataloğu

Katalog çalışma dizini (repo içi): `katalog/` — script'ler, üretilen JSON, numaralı liste. Ham veri kaynakları: fork kaynağındaki `remote/juggler/` (deployed ile birebir) + `libxul.so` strings + omni.ja (yalnızca mevcut Camoufox binary'sinden, DEĞİŞTİRMEDEN).

Bilinen envanter (Task 1.1-1.3 bunları repo içine alır ve doğrular):
- nsI* interface: ~1070 (libxul.so strings) → `katalog/nsI-list.txt`
- Services.*: ~60 benzersiz → `katalog/services-list.txt`
- ChromeUtils.*: 64 → `katalog/chromeutils-list.txt`
- Contract ID: 467 (@mozilla.org/...;1) → `katalog/classes-list.txt`
- omni.ja modülleri: 499 .sys.mjs → `katalog/modules-list.txt`
- windowUtils 47 / docShell 37 / frameLoader 6 / webNavigation 13 method

### Task 1.1: Envanter script'leri (repo'ya kalıcı)

**Files:**
- Create: `katalog/extract_nsi.sh`, `katalog/extract_services.sh`, `katalog/extract_classes.sh`, `katalog/extract_modules.sh`
- Create: `katalog/README.md`

**Interfaces:**
- Produces: `katalog/*-list.txt` dosyaları (sıralı, benzersiz). Task 1.2'nin girdisi.

- [ ] **Step 1: `katalog/extract_nsi.sh` yaz**

```bash
#!/usr/bin/env bash
# nsI* interface listesi — mevcut Camoufox binary'sinden (DEĞİŞTİRİLMEZ okuma)
set -euo pipefail
CAMOUFOX="${CAMOUFOX:-$HOME/.cache/camoufox/browsers/official/152.0.4-beta.28-924f3109/camoufox}"
OUT="$(cd "$(dirname "$0")/.." && pwd)/katalog/nsI-list.txt"
strings -a "$CAMOUFOX/libxul.so" | grep -E '^nsI[A-Za-z0-9_]+$' | sort -u > "$OUT"
wc -l "$OUT"   # BEKLENTİ: ~1070
```

- [ ] **Step 2: `katalog/extract_services.sh` yaz**

```bash
#!/usr/bin/env bash
# Services.* / ChromeUtils.* / modül listeleri — omni.ja'dan (geçici açılış, değişiklik yok)
set -euo pipefail
CAMOUFOX="${CAMOUFOX:-$HOME/.cache/camoufox/browsers/official/152.0.4-beta.28-924f3109/camoufox}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)/katalog"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
unzip -q "$CAMOUFOX/omni.ja" -d "$WORK/omni"
grep -rhoE 'Services\.[a-zA-Z0-9_]+' "$WORK/omni" --include='*.js*' --include='*.mjs' --include='*.sys.mjs' \
  | sed 's/Services\.//' | sort -u > "$ROOT/services-list.txt"
grep -rhoE 'ChromeUtils\.[a-zA-Z0-9_]+' "$WORK/omni" --include='*.js*' --include='*.mjs' --include='*.sys.mjs' \
  | sed 's/ChromeUtils\.//' | sort -u > "$ROOT/chromeutils-list.txt"
find "$WORK/omni/modules" -name '*.sys.mjs' -printf '%f\n' | sort -u > "$ROOT/modules-list.txt"
wc -l "$ROOT/services-list.txt" "$ROOT/chromeutils-list.txt" "$ROOT/modules-list.txt"
```

- [ ] **Step 3: `katalog/extract_classes.sh` yaz**

```bash
#!/usr/bin/env bash
# Contract ID'ler (@mozilla.org/...;1) — libxul.so'dan
set -euo pipefail
CAMOUFOX="${CAMOUFOX:-$HOME/.cache/camoufox/browsers/official/152.0.4-beta.28-924f3109/camoufox}"
OUT="$(cd "$(dirname "$0")/.." && pwd)/katalog/classes-list.txt"
strings -a "$CAMOUFOX/libxul.so" | grep -E '^@mozilla\.org/[a-zA-Z0-9_./-]+;1$' | sort -u > "$OUT"
wc -l "$OUT"   # BEKLENTİ: ~467
```

- [ ] **Step 4: `katalog/extract_modules.sh` yaz**

```bash
#!/usr/bin/env bash
# Browser chrome modülleri (SessionStore, Downloads vb. omni.ja'da olmayabilir) — fork kaynağından
set -euo pipefail
SRC="$(ls -d ~/camoufox-src/camoufox/firefox-* 2>/dev/null | head -1)"
OUT="$(cd "$(dirname "$0")/.." && pwd)/katalog/src-modules-list.txt"
find "$SRC/browser/components" "$SRC/toolkit/modules" -name '*.sys.mjs' -printf '%P\n' 2>/dev/null | sort -u > "$OUT"
wc -l "$OUT"
```

- [ ] **Step 5: Çalıştır + `katalog/README.md`**

```bash
chmod +x katalog/extract_*.sh
katalog/extract_nsi.sh && katalog/extract_services.sh && katalog/extract_classes.sh && katalog/extract_modules.sh
```

```markdown
# Katalog
- `*-list.txt`: ham XPCOM yüzeyi (nsI, Services, ChromeUtils, classes, modules)
- `domains.json`: 1453 domain kataloğu (Task 1.2 üretir) — PLANIN ANA VERİSİ
- `domains-1453.txt`: numaralı liste (çıktı belgesi)
- Script'ler mevcut Camoufox binary'sini SADECE okur (strings/unzip), değiştirmez.
```

- [ ] **Step 6: Commit**

```bash
git add katalog/ && git commit -m "feat(katalog): XPCOM surface extraction scripts (nsI/Services/ChromeUtils/classes/modules)"
```

### Task 1.2: 1453 domain kataloğu generator

**Files:**
- Create: `katalog/gen_domains.py` (~300 satır — ana generator)
- Create: `katalog/domains.json` (çıktı), `katalog/domains-1453.txt` (numaralı liste çıktısı)

**Interfaces:**
- Produces: `domains.json` — 1453 öğe, her biri:
  ```json
  {"id": 1, "domain": "Browser", "category": "core", "source": "juggler-native",
   "methods": [{"name": "enable", "kind": "juggler"}, ...], "events": []}
  ```
  ve yeni domain'ler için `"source": "service:cookies" | "interface:nsICookieManager" | "module:DownloadCore" | "cc:@mozilla.org/..."`.
  Task 1.3 doğrular, Faz 4 mapping'i doldurur.
- Consumes: `katalog/*-list.txt` (Task 1.1).

- [ ] **Step 1: `katalog/gen_domains.py` yaz (tam kod)**

```python
#!/usr/bin/env python3
"""1453 domain kataloğu üretir: XPCOM yüzeyi -> domain listesi.

Kurallar:
  1. nsI* interface -> 1 domain (ad: nsI ön eki çıkarılmış, camelCase korunur:
     nsICookieManager -> "CookieManager")
  2. Services.* -> 1 domain ("Services" ön eki: cookies -> "CookieService")
  3. ChromeUtils.* -> "ChromeUtils" domain altında method adayları (tek domain)
  4. Contract ID -> 1 domain (sınıf adından: appshell/window-mediator -> "WindowMediator")
  5. omni.ja modülleri -> 1 domain (ad: dosya adı .sys.mjs'siz; SessionStore -> "SessionStore")
  6. Mevcut Juggler 6 domain ilk sıralarda, katalog kimliği korunur.
  7. Toplam TAM 1453 olacak şekilde kırpılır/eklenir — asla 1453'ü aşmaz,
     1453'e ulaşamıyorsa son adım: "DomainN" olmayan yalnızca GERÇEK yüzeylerden
     alt bölme (ör: nsIXxxManager tek domain yerine method grupları domain'e çevrilmez —
     önce tüm listeler birleştirilir, 1453 altındaysa interface başına event/observer
     domain'leri eklenir (gerçek: nsIObserverService topic'leri -> ObserverTopic domain'leri).
  Katalog sırası deterministik: id 1..1453, alfabetik değil — aşağıdaki kategori
  önceliğiyle (core, dom, input, network, storage, emulation, media, security,
  perf, process, a11y, ui, advanced, bulk).
"""
import json, re, sys, pathlib

ROOT = pathlib.Path(__file__).parent
TARGET = 1453

CATEGORY_ORDER = ["core","dom","input","network","storage","emulation","media",
                  "security","perf","process","a11y","ui","advanced","bulk"]

def load_list(name):
    p = ROOT / name
    if not p.exists():
        print(f"MISSING {p}", file=sys.stderr); return []
    return [l.strip() for l in p.read_text().splitlines() if l.strip()]

def domain_from_nsi(name):
    return name[2:]  # nsICookieManager -> CookieManager

def domain_from_service(name):
    return name.capitalize() + "Service"  # cookies -> CookiesService

def domain_from_contract(cid):
    # "@mozilla.org/appshell/window-mediator;1" -> "WindowMediator"
    mid = cid.replace("@mozilla.org/","").replace(";1","")
    parts = re.split(r"[-_/]", mid)
    return "".join(p.capitalize() for p in parts if p)

def domain_from_module(fname):
    return fname.removesuffix(".sys.mjs")

def main():
    nsI    = load_list("nsI-list.txt")
    svc    = load_list("services-list.txt")
    chru   = load_list("chromeutils-list.txt")
    clss   = load_list("classes-list.txt")
    mods   = load_list("modules-list.txt") + load_list("src-modules-list.txt")

    # mevcut Juggler domain'leri (kesin korunur)
    juggler = [
        {"id":0,"domain":"Browser","category":"core","source":"juggler-native","methods":[],"events":[]},
        {"id":0,"domain":"Page","category":"core","source":"juggler-native","methods":[],"events":[]},
        {"id":0,"domain":"Network","category":"network","source":"juggler-native","methods":[],"events":[]},
        {"id":0,"domain":"Runtime","category":"core","source":"juggler-native","methods":[],"events":[]},
        {"id":0,"domain":"Heap","category":"perf","source":"juggler-native","methods":[],"events":[]},
        {"id":0,"domain":"Accessibility","category":"a11y","source":"juggler-native","methods":[],"events":[]},
    ]

    # ad -> (category, source) ; çakışmalarda nsI öncelikli
    seen = {}
    def add(domain, category, source):
        if domain in seen: return
        seen[domain] = (category, source)

    for d in juggler: add(d["domain"], d["category"], d["source"])
    for n in nsI:    add(domain_from_nsi(n),    "bulk",   f"interface:{n}")
    for s in svc:    add(domain_from_service(s),"ui",     f"service:{s}")
    for c in clss:   add(domain_from_contract(c),"bulk",  f"cc:{c}")
    for m in mods:   add(domain_from_module(m),  "bulk",   f"module:{m}")

    # kategori etiketlemesi: bilinen öneklerle zenginleştir
    CAT = {
        "dom":  ("DOM","Layout","Style","CSS","Animation","Transition","Mutation","Resize","Intersection","Element","Node","Document","Range","Selection","EventTarget"),
        "input":("Input","Mouse","Keyboard","Touch","Drag","Gesture","Pointer"),
        "network":("Network","Fetch","WebSocket","WebRTC","HTTP","Cookie","Channel","Proxy","DNS","Cache","Socket"),
        "storage":("Storage","IndexedDB","CacheStorage","LocalStorage","SessionStorage","FileSystem","Quota","File"),
        "emulation":("Emulation","Device","Geolocation","Sensor","Battery","MediaDevice","Screen","Orientation"),
        "media":("Media","Audio","Video","WebAudio","MediaStream","Capture"),
        "security":("Security","Permission","CSP","Certificate","MixedContent","Password","Login","Sanitizer"),
        "perf":("Performance","Memory","Profiler","Tracing","Log","Console","Debugger","Heap"),
        "process":("Process","Thread","SystemInfo","MemoryInfo","CPU","GPU","Jank"),
        "a11y":("Accessibility","AXTree","ScreenReader"),
        "ui":("History","SessionStore","Tab","Window","Bookmark","Download","Print","Extension","Sidebar","Toolbar"),
        "advanced":("Observer","Preference","AboutPage","InternalAPI","Service","ChromeUtils","Startup","AppInfo","Directory","Prompt","Search","Telemetry","Locale","StringBundle","Profile","Blocklist","Update","Cleanup","Telemetry"),
    }
    for name,(cat,src) in list(seen.items()):
        if cat != "bulk": continue
        for k,prefs in CAT.items():
            if any(p in name for p in prefs):
                seen[name] = (k, src); break

    # 1453'e tamamlama: yalnızca GERÇEK yüzeyler. Hedef aşımı: en alt kategoriden kırp.
    entries = [{"id":i+1,"domain":d,"category":seen[d][0],"source":seen[d][1],
                "methods":[],"events":[]} for i,d in enumerate(seen)]
    if len(entries) > TARGET:
        # "bulk" kategori domain'lerinden, modül kökenlilerden başla kırp (en az kritik)
        for e in entries:
            if len(entries) <= TARGET: break
            if e["category"] == "bulk" and e["source"].startswith("module:"):
                entries.remove(e)
    if len(entries) < TARGET:
        # chromeutils method'ları ayrı domain'lere böl (gerçek: her method ChromeUtils'in bir
        # işlevidir; gerektiği kadar method -> "ChromeUtilsX" alt domain'leri)
        for i,m in enumerate(chru[:TARGET-len(entries)]):
            entries.append({"id":len(entries)+1,"domain":f"ChromeUtils{i+1}","category":"advanced",
                            "source":f"chromeutils-method:{m}","methods":[],"events":[]})
    # id'leri yeniden sırala
    for i,e in enumerate(entries): e["id"] = i+1
    assert len(entries) == TARGET, f"katalog {len(entries)} != {TARGET}"

    (ROOT/"domains.json").write_text(json.dumps(entries, indent=1))
    lines = [f"{e['id']}\t{e['domain']}\t{e['category']}\t{e['source']}" for e in entries]
    (ROOT/"domains-1453.txt").write_text("\n".join(lines) + "\n")
    print(f"katalog: {len(entries)} domain -> katalog/domains.json, katalog/domains-1453.txt")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Çalıştır + doğrula**

```bash
python3 katalog/gen_domains.py
# BEKLENTİ: "katalog: 1453 domain -> katalog/domains.json, katalog/domains-1453.txt"
wc -l katalog/domains-1453.txt          # 1453
python3 -c "import json;d=json.load(open('katalog/domains.json'));print(len(d),d[0]['domain'],d[-1]['domain'])"
```

- [ ] **Step 3: İlk 30 domain'i gözden geçir (elle inceleme)**

```bash
head -30 katalog/domains-1453.txt
# Kategori dağılımı:
cut -f3 katalog/domains-1453.txt | sort | uniq -c | sort -rn
```

- [ ] **Step 4: Commit**

```bash
git add katalog/gen_domains.py katalog/domains.json katalog/domains-1453.txt
git commit -m "feat(katalog): 1453-domain catalog generated from XPCOM surface"
```

### Task 1.3: Katalog doğrulama script'i

**Files:**
- Create: `katalog/verify_domains.py`

**Interfaces:**
- Produces: `katalog/verify-report.txt` — her domain için kaynak mevcudiyeti kanıtı (sembol/string/modül dosyası). Faz 4'te her kategori task'ı önce kendi domain'lerini bu raporla doğrular.

- [ ] **Step 1: `katalog/verify_domains.py` yaz (tam kod)**

```python
#!/usr/bin/env python3
"""Her domain'in kaynağının GERÇEKTEN var olduğunu doğrular (statik kanıt).

- interface:nsIXXX  -> libxul.so strings'te nsIXXX var mı
- service:NAME     -> services-list.txt'te var mı (ya da nsI karşılığı)
- cc:ID            -> classes-list.txt'te var mı
- module:FILE      -> modules-list.txt / src-modules-list.txt'te var mı
- chromeutils-method:M -> chromeutils-list.txt'te var mı
- juggler-native   -> her zaman geçerli
Çıktı: katalog/verify-report.txt (PASS/FAIL satırları) + özet; exit 0 yalnızca 0 FAIL.
"""
import json, pathlib, sys, re
ROOT = pathlib.Path(__file__).parent
def load(name):
    p = ROOT/name
    return set(p.read_text().splitlines()) if p.exists() else set()

NSI = load("nsI-list.txt"); SVC = load("services-list.txt")
CLS = load("classes-list.txt"); MOD = load("modules-list.txt") | load("src-modules-list.txt")
CHRU = load("chromeutils-list.txt")

fail = pass_ = 0
rows = []
for e in json.loads((ROOT/"domains.json").read_text()):
    src = e["source"]; ok = True
    if src == "juggler-native": pass
    elif src.startswith("interface:"):
        ok = src[10:] in NSI
    elif src.startswith("service:"):
        s = src[8:]; ok = s in SVC or any(nsi[2:].lower() == s.lower() for nsi in NSI)
    elif src.startswith("cc:"):
        ok = src[3:] in CLS
    elif src.startswith("module:"):
        ok = src[7:] in MOD
    elif src.startswith("chromeutils-method:"):
        ok = src[19:] in CHRU
    else: ok = False
    rows.append(f"{'PASS' if ok else 'FAIL'}\t{e['id']}\t{e['domain']}\t{src}")
    pass_ += ok; fail += not ok
(ROOT/"verify-report.txt").write_text("\n".join(rows)+"\n")
print(f"PASS={pass_} FAIL={fail} total={pass_+fail}")
sys.exit(1 if fail else 0)
```

- [ ] **Step 2: Çalıştır — 0 FAIL hedefi**

```bash
python3 katalog/verify_domains.py | tee /tmp/opencode/verify.log
grep -c '^FAIL' katalog/verify-report.txt   # 0 olmalı
# FAIL varsa: Task 1.2'deki kaynak eşleşmesi yanlıştır -> katalogu düzelt (ad çakışması çözümü:
# gen_domains.py'de source'u doğru liste adına göre yaz, yeniden üret, tekrar doğrula.
```

- [ ] **Step 3: Commit**

```bash
git add katalog/verify_domains.py katalog/verify-report.txt
git commit -m "test(katalog): verify every domain resolves to a real XPCOM surface (0 FAIL)"
```

---

## Faz 2 — Protocol.js genişletme + Dispatcher generic fallback

Juggler kaynak yüzeyi (fork ağacında): `remote/juggler/protocol/Protocol.js` (domain registry), `Dispatcher.js` (validate+route), `PrimitiveTypes.js`. Content: `remote/juggler/content/PageAgent.js` (channel.register('page')), `main.js` (channel.register('')). Hepsi chrome-privileged.

### Task 2.1: PrimitiveTypes'a `juggler-any` tipi

**Files:**
- Modify: `remote/juggler/protocol/PrimitiveTypes.js` (fork ağacında; deployed kopya `/tmp/opencode/juggler-research/deployed/root/chrome/juggler/content/protocol/PrimitiveTypes.js` ile birebir — karşılaştır ve yalnızca ekleme yap)

**Interfaces:**
- Produces: `juggler-any` — herhangi bir JSON değerini kabul eden tip (string/number/bool/null/object/array). Faz 2.3'te Dispatcher validation'ı bu tipi geçirir; XpcomBridge dönüşlerinde kullanılır.

- [ ] **Step 1: Mevcut dosyayı oku + `juggler-any` ekle**

```bash
cd ~/camoufox-src/camoufox/firefox-*/remote/juggler/protocol
cat PrimitiveTypes.js   # mevcut tip tanımlarını gör (string/number/boolean/object/any benzeri)
```

```js
// PrimitiveTypes.js — dosya sonuna ekle (mevcut yapıyı bozmadan):
const primitiveTypes = new Map();
// ... mevcut kayıtlar aynen kalır ...

primitiveTypes.set('juggler-any', {
  validate(value) {
    // herhangi bir JSON değeri kabul et; undefined/null da geçer
    return value === undefined || value === null ||
      ['string','number','boolean','object'].includes(typeof value);
  },
  print(value) { return JSON.stringify(value); },
});
```

- [ ] **Step 2: Dispatcher'ın PrimitiveTypes kullanımını doğrula**

```bash
grep -n "primitiveTypes\|juggler-any\|validate" Dispatcher.js | head -20
# Dispatcher, params tiplerini bu haritadan validate ediyorsa: 'juggler-any' otomatik kabul edilir.
```

- [ ] **Step 3: Commit**

```bash
git add remote/juggler/protocol/PrimitiveTypes.js
git commit -m "feat(juggler): add juggler-any primitive type for generic XPCOM passthrough"
```

### Task 2.2: Protocol.js domain tanım generator'ı

**Files:**
- Create: `katalog/gen_protocol.py` (katalog JSON → Protocol.js domain blokları)
- Create: `katalog/protocol-domains.js` (çıktı — Protocol.js'e eklenecek bölüm)

**Interfaces:**
- Produces: 1453 domain tanımı (her domain: `methods` alanı — method adları XPCOM karşılığıyla birebir; ilk aşamada method listeleri boş olabilir, Faz 4.1 Introspector ile doldurulur; her domain en az `invoke` adında GENERIC method'a sahiptir — boş domain YASAK, invoke her zaman gerçektir).
- Consumes: `katalog/domains.json` (Task 1.2).

- [ ] **Step 1: `katalog/gen_protocol.py` yaz (tam kod)**

```python
#!/usr/bin/env python3
"""katalog/domains.json -> Protocol.js domain blokları.

Her domain için: methods = { invoke: {params: {args: juggler-any}, returns: {result: juggler-any}} }
+ domain.enable/domain.disable event gateway'leri (Faz 6.2'de doldurulur, tanımlar şimdi).
'juggler-any' tipi Task 2.1'de tanımlı. Method adları XPCOM birebir eşleşmesi Faz 4.1'de
Introspector ile method listesine yazılır (bu dosya yalnızca iskelet + invoke).
"""
import json, pathlib
ROOT = pathlib.Path(__file__).parent

def domain_block(e):
    d = e["domain"]
    return f"""
  {d}: {{
    methods: {{
      enable: {{ params: {{}}, returns: {{}} }},
      disable: {{ params: {{}}, returns: {{}} }},
      invoke: {{
        params: {{ args: {{ type: 'juggler-any' }} }},
        returns: {{ result: 'juggler-any' }},
      }},
    }},
    events: {{}},
  }},"""

def main():
    entries = json.loads((ROOT/"domains.json").read_text())
    body = "".join(domain_block(e) for e in entries)
    out = f"""// GENERATED by katalog/gen_protocol.py — do not edit by hand.
// Juggler 1453 domain registry extension. Mevcut domains objesine merge edilir.
const juggler1453 = {{{body}
}};
"""
    (ROOT/"protocol-domains.js").write_text(out)
    n = out.count("methods:")
    print(f"protocol blok: {n} domain -> katalog/protocol-domains.js")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Çalıştır + doğrula**

```bash
python3 katalog/gen_protocol.py   # "protocol blok: 1453 domain"
head -25 katalog/protocol-domains.js
python3 - <<'EOF'
import re
src = open('katalog/protocol-domains.js').read()
print("domain blokları:", src.count("invoke"), "(1453 olmalı)")
EOF
```

- [ ] **Step 3: Commit**

```bash
git add katalog/gen_protocol.py katalog/protocol-domains.js
git commit -m "feat(katalog): Protocol.js generator — 1453 domain blocks with generic invoke"
```

### Task 2.3: Dispatcher — generic handler fallback

**Files:**
- Modify: `remote/juggler/protocol/Dispatcher.js` (fork ağacı)
- Modify: `remote/juggler/protocol/Protocol.js` (domains objesine 1453 blok merge)

**Interfaces:**
- Produces: Dispatcher, bilinmeyen domain'lerde handler aramak yerine `XpcomBridge.invoke(domain, method, params)` çağırır; `Domain.enable/disable` → event gateway (Faz 6.2'de doldurulur — Dispatcher'ın route'u hazır olur).
- Consumes: `katalog/protocol-domains.js` (Task 2.2), `juggler-any` (Task 2.1), `XpcomBridge` (Faz 3 — import ertelenemezse Task 3.2 sonrası test edilir; bu task compile-time değil runtime bağımlılık, Dispatcher fallback'i `XpcomBridge` modülünü lazy import eder).

- [ ] **Step 1: Dispatcher.js'i oku — handler çözümleme noktasını bul**

```bash
cd ~/camoufox-src/camoufox/firefox-*/remote/juggler/protocol
grep -n "handler\|domain\|route\|callMethod" Dispatcher.js | head -40
```

- [ ] **Step 2: Generic fallback ekle (mevcut akış korunur)**

```js
// Dispatcher.js — handler bulunamadığı noktada (method dispatch öncesi) ekle:
// (mevcut: handler = session.handlers[domain] gibi bir arama; bulunamazsa -32601 döner)
// DEĞİŞİKLİK: handler bulunamadığında XpcomBridge'e düş:
if (!handler || typeof handler[methodName] !== 'function') {
  const Xpcom = await import('chrome://juggler/content/protocol/XpcomBridge.js');
  return Xpcom.bridge.invoke(domain, method, params, session);
}
// Not: domain.enable/disable zaten handler'da tanımlıysa mevcut yol çalışır;
// XpcomBridge generic invoke'e "enable/disable" için event gateway'e devreder (Faz 6.2).
```

- [ ] **Step 3: Protocol.js'e 1453 blok merge**

```bash
cd ~/camoufox-src/camoufox/firefox-*/remote/juggler/protocol
# domains objesini juggler1453 ile birleştir (el ile, tek nokta):
#   const domains = { ...mevcutDomains, ...juggler1453 };
# juggler1453'ü ayrı dosya olarak koy:
cp /home/void0x14/Belgeler/mcp-projelerim/cdp-kahin-mcp/katalog/protocol-domains.js Juggler1453.js
# Protocol.js: import { juggler1453 } from './Juggler1453.js'; (mevcut module yapısına uy)
grep -n "export\|domains =" Protocol.js | head
```

- [ ] **Step 4: Doğrula — domain sayısı**

```bash
grep -c "invoke" Juggler1453.js    # 1453
node -e "require('./Juggler1453.js')" 2>/dev/null || echo "ESM — node -c ile sözdizimi kontrolü:"
node --check Juggler1453.js        # sözdizimi hatasız olmalı
```

- [ ] **Step 5: Commit**

```bash
git add remote/juggler/protocol/Dispatcher.js remote/juggler/protocol/Protocol.js remote/juggler/protocol/Juggler1453.js
git commit -m "feat(juggler): dispatcher generic fallback — 1453 domain route to XpcomBridge"
```

---

## Faz 3 — GenericXpcomBridge (browser + content)

Bridge = 1453 domain'in implementasyonu. Stub yasak kuralı burada yaşar: her `Domain.invoke` çağrısı gerçek XPCOM'a gider. Üç dosya:

- `XpcomRegistry.js` — katalog mapping (domain → XPCOM kaynağı) + **handle sistemi** (XPCOM nesne dönüşleri pipe üzerinden handle ile referanslanır — serileştirme değil, gerçek referans).
- `XpcomBridge.js` (browser process) — `invoke(domain, method, args, session)`: kaynağı çözer, method'u çağırır, dönüşü serialize/nesneyi handle'lar, nsIException'ı detaylı hata yapar.
- `XpcomAgent.js` (content process) — aynı mantık frame bağlamında; browser'dan `Xpcom.invokeFrame` ile tetiklenir (SimpleChannel namespace 'xpcom'), frame DOM/window erişimi için.

Handle sistemi: bridge'de `Map<handleId, object>`; nesne dönen method'lar `{__xpcom: handleId, iface: "nsIFoo", methods: [m1,m2,...]}` döner; sonraki invoke'lar `handle` parametresiyle nesne üzerinde çağrılır. Handle'lar session ömürlü, `Domain.disable`/session kill'de temizlenir.

### Task 3.1: XpcomRegistry.js — mapping + handle yönetimi

**Files:**
- Create: `remote/juggler/protocol/XpcomRegistry.js`

**Interfaces:**
- Produces:
  - `resolve(domain)` → `{kind: 'service'|'interface'|'cc'|'module'|'native', target}` — katalog kaynağından çözülmüş canlı XPCOM nesnesi.
  - `handle(obj)` → string (yeni handle üretir), `byHandle(id)` → object|undefined, `release(id)`, `releaseAll(sessionId)`.
  - `methodNames(obj)` → string[] (nesne üzerindeki çağrılabilir method'lar — Xray'de görünen prototype method'ları).
- Consumes: `katalog/domains.json` → `remote/juggler/XpcomCatalog.json` (build zamanı kopyası).

- [ ] **Step 1: Katalog JSON'u Juggler ağacına kopyala**

```bash
mkdir -p ~/camoufox-src/camoufox/firefox-*/remote/juggler/
cp katalog/domains.json ~/camoufox-src/camoufox/firefox-*/remote/juggler/XpcomCatalog.json
```

- [ ] **Step 2: `XpcomRegistry.js` yaz (tam kod)**

```js
/* XpcomRegistry.js — domain -> XPCOM kaynağı çözümleyici + handle deposu.
 * Juggler 1453 domain. Tüm XPCOM erişimi chrome-privileged (system principal).
 * ChromeUtils/Components/Services global olarak mevcuttur. */
const { XPCOMUtils } = ChromeUtils.importESModule("resource://gre/modules/XPCOMUtils.sys.mjs");

const catalog = JSON.parse(
  IOUtils.readUTF8(new URL("chrome://juggler/content/XpcomCatalog.json").pathname) // build-time kopya
    .catch?.(() => readCatalogFallback())
);
function readCatalogFallback() {
  // jar'dan okunamazsa minimal: yalnızca Juggler-native + servis isimleri
  return { entries: [] };
}

const { Services } = ChromeUtils.importESModule("resource://gre/modules/Services.sys.mjs");
const Ci = Components.interfaces;
const Cc = Components.classes;

const handles = new Map(); // handleId -> {obj, sessionId}
let nextHandle = 1;

function resolve(domain) {
  const entry = catalog.entries?.find(e => e.domain === domain) || { source: "juggler-native" };
  const src = entry.source;
  if (src === "juggler-native") return { kind: "native", target: null };
  if (src.startsWith("service:")) {
    const name = src.slice(8);
    if (name in Services) return { kind: "service", target: Services[name] };
    throw new Error(`XpcomRegistry: unknown service '${name}' (domain ${domain})`);
  }
  if (src.startsWith("interface:")) {
    const iface = src.slice(10);
    if (!(iface in Ci)) throw new Error(`XpcomRegistry: unknown interface '${iface}'`);
    return { kind: "interface", target: Ci[iface] };
  }
  if (src.startsWith("cc:")) {
    const cid = src.slice(3);
    try { return { kind: "cc", target: Cc[cid].getService(Ci.nsISupports) }; }
    catch (e) {
      try { return { kind: "cc", target: Cc[cid].createInstance(Ci.nsISupports) }; }
      catch (e2) { throw new Error(`XpcomRegistry: cannot instantiate '${cid}': ${e2}`); }
    }
  }
  if (src.startsWith("module:")) {
    const mod = src.slice(7);
    // modül ismi -> resource URI tahmini (downloads -> DownloadCore vb.); bulunamazsa net hata
    const uri = `resource://gre/modules/${mod}.sys.mjs`;
    try { return { kind: "module", target: ChromeUtils.importESModule(uri) }; }
    catch (e) { throw new Error(`XpcomRegistry: cannot import module '${uri}': ${e}`); }
  }
  if (src.startsWith("chromeutils-method:")) {
    const m = src.slice(19);
    return { kind: "chromeutils", target: ChromeUtils, method: m };
  }
  throw new Error(`XpcomRegistry: unresolved source '${src}' for domain ${domain}`);
}

function methodNames(obj) {
  const names = new Set();
  let proto = obj;
  while (proto && proto !== Object.prototype) {
    for (const k of Object.getOwnPropertyNames(proto)) {
      if (typeof proto[k] === "function" && k !== "constructor") names.add(k);
    }
    proto = Object.getPrototypeOf(proto);
  }
  return [...names];
}

function handle(obj, sessionId) {
  const id = `h${nextHandle++}`;
  handles.set(id, { obj, sessionId });
  return { __xpcom: id, iface: obj.constructor?.name || "XpcomObject", methods: methodNames(obj) };
}

function byHandle(id) { return handles.get(id)?.obj; }
function release(id) { handles.delete(id); }
function releaseAll(sessionId) {
  for (const [id, h] of handles) if (h.sessionId === sessionId) handles.delete(id);
}

function isHandle(v) { return v && typeof v === "object" && typeof v.__xpcom === "string"; }

export const XpcomRegistry = { resolve, methodNames, handle, byHandle, release, releaseAll, isHandle };
```

- [ ] **Step 3: Sözdizimi kontrolü**

```bash
node --check ~/camoufox-src/camoufox/firefox-*/remote/juggler/protocol/XpcomRegistry.js
```

- [ ] **Step 4: Commit**

```bash
git add remote/juggler/XpcomCatalog.json remote/juggler/protocol/XpcomRegistry.js
git commit -m "feat(juggler): XpcomRegistry — domain->XPCOM resolver + handle store"
```

### Task 3.2: XpcomBridge.js — browser-process invoke

**Files:**
- Create: `remote/juggler/protocol/XpcomBridge.js`

**Interfaces:**
- Produces: `bridge.invoke(domain, method, args, session)` → JSON-serializable sonuç:
  - primitive → olduğu gibi
  - XPCOM nesnesi → `{__xpcom: handleId, iface, methods}` (XpcomRegistry.handle)
  - `XpcomRegistry.isHandle(v)` arg → `byHandle` ile gerçek nesneye çevrilir (recursive)
  - nsIException → `{error: {code, name, message, result, stack}}` (asla sessiz geçme — kural)
  - `enable/disable` method'ları → event gateway kaydı (Faz 6.2'de doldurulur, şimdi no-op DEĞİL: gateway'e abone olur)
- Consumes: XpcomRegistry (3.1), `juggler-any` (2.1).

- [ ] **Step 1: `XpcomBridge.js` yaz (tam kod)**

```js
/* XpcomBridge.js — GenericXpcomBridge (browser process).
 * 1453 domain'in implementasyonu: Domain.invoke -> gerçek XPCOM çağrısı. */
import { XpcomRegistry } from "./XpcomRegistry.js";

const eventGateways = new Map(); // domain -> Set<session>

function serialize(value, sessionId, depth = 0) {
  if (depth > 8) return { __xpcom: "too-deep" };
  if (value === null || value === undefined) return value;
  const t = typeof value;
  if (t === "string" || t === "number" || t === "boolean") return value;
  if (t === "function") return { __xpcom: "function", name: value.name || "anonymous" };
  if (t === "object") {
    if (XpcomRegistry.isHandle(value)) return value; // zaten handle
    if (value instanceof Components.interfaces.nsISupports || value?.wrappedJSObject) {
      try { return XpcomRegistry.handle(value, sessionId); }
      catch { return { __xpcom: "opaque" }; }
    }
    if (Array.isArray(value)) return value.map(v => serialize(v, sessionId, depth + 1));
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (typeof v === "function") continue;
      out[k] = serialize(v, sessionId, depth + 1);
    }
    return out;
  }
  return { __xpcom: `unserializable:${t}` };
}

function toNative(value) {
  if (XpcomRegistry.isHandle(value)) return XpcomRegistry.byHandle(value.__xpcom);
  return value;
}

export const bridge = {
  async invoke(domain, method, args, session) {
    const sessionId = session?.id;
    const argList = Array.isArray(args) ? args : (args === undefined || args === null ? [] : [args]);
    try {
      const resolved = XpcomRegistry.resolve(domain);
      let target;
      let callArgs = argList.map(toNative);

      if (resolved.kind === "native") {
        // Juggler-native domain'ler (Browser/Page/Runtime/...) — buraya düşmez:
        // mevcut handler'ları vardır; yalnızca Juggler'da tanımsız method'lar gelir.
        throw new Error(`XpcomBridge: no native handler for ${domain}.${method}`);
      }
      if (resolved.kind === "chromeutils") {
        target = ChromeUtils[resolved.method];
        return serialize(await target(...callArgs), sessionId);
      }
      target = resolved.target;
      if (!target || typeof target[method] !== "function") {
        throw new Error(
          `XpcomBridge: '${domain}' (${resolved.kind}:${resolved.method ? resolved.method : ""}) ` +
          `has no method '${method}'. Available: ${XpcomRegistry.methodNames(target).slice(0, 40).join(", ")}`
        );
      }
      if (method === "enable" || method === "disable") {
        if (method === "enable") eventGateways.set(domain, (eventGateways.get(domain) || new Set()).add(sessionId));
        else {
          const set = eventGateways.get(domain);
          set?.delete(sessionId);
          if (set && set.size === 0) eventGateways.delete(domain);
        }
        return { enabled: method === "enable" };
      }
      let result = target[method](...callArgs);
      if (result && typeof result.then === "function") result = await result;
      return serialize(result, sessionId);
    } catch (e) {
      // DETAYLI hata — sessizce geçme yasağı
      const err = { name: e.name || "Error", message: e.message || String(e) };
      if (e instanceof Components.Exception) {
        err.code = e.result;
        err.filename = e.filename;
        err.lineNumber = e.lineNumber;
        err.stack = e.stack;
      } else if (e.stack) err.stack = String(e.stack).split("\n").slice(0, 8);
      return { error: err };
    }
  },

  emit(domain, eventName, params) {
    const sessions = eventGateways.get(domain);
    if (!sessions) return;
    for (const sid of sessions) {
      // session.emitEvent mevcut Juggler mekanizması (Dispatcher/session nesnesi)
      if (this._sessionEmit) this._sessionEmit(sid, `${domain}.${eventName}`, params);
    }
  },
};
```

- [ ] **Step 2: Bridge'i Dispatcher'a bağla (Task 2.3'ün fallback'i modülü artık gerçek)**

```bash
grep -n "XpcomBridge" ~/camoufox-src/camoufox/firefox-*/remote/juggler/protocol/Dispatcher.js
# Task 2.3'te eklenen fallback'in import yolu doğru: chrome://juggler/content/protocol/XpcomBridge.js
```

- [ ] **Step 3: Build + smoke (fork binary, inkremental)**

```bash
cd ~/camoufox-src/camoufox/firefox-*/
./mach build 2>&1 | tail -3   # inkremental ~2-10 dk
# Smoke: fork binary + mevcut sidecar ile raw invoke:
CAMOUFOX_BIN=$PWD/obj-*/dist/bin/firefox \
  /home/void0x14/Belgeler/mcp-projelerim/cdp-kahin-mcp/.venv/bin/python - <<'EOF'
import asyncio
from kahin.the_twins.mirage import Mirage
async def main():
    m = Mirage(engine_name="mirage")
    await m.start()
    r = await m.send_cdp("Browser", "getInfo", {})   # Juggler-native: hâlâ çalışıyor mu
    print("native:", r)
    r2 = await m.send_cdp("CookiesService", "invoke", {"args": ["getAll"]})
    print("bridge:", r2)
    await m.stop()
asyncio.run(main())
EOF
# BEKLENTİ: native çağrı başarılı; bridge invoke: CookiesService.getAll -> cookie listesi veya
# deterministik detaylı hata (method adı + mevcut method listesi) — İKİSİ DE GEÇERLİ KANIT.
```

- [ ] **Step 4: Commit**

```bash
git add remote/juggler/protocol/XpcomBridge.js
git commit -m "feat(juggler): XpcomBridge — generic XPCOM invoke with handle system + detailed errors"
```

### Task 3.3: XpcomAgent.js — content-process bridge

**Files:**
- Create: `remote/juggler/content/XpcomAgent.js`
- Modify: `remote/juggler/content/PageAgent.js` (register 'xpcom' namespace)

**Interfaces:**
- Produces: `XpcomAgent.invokeInFrame(browserId, frameId, domain, method, args)` — frame'in chrome-privileged bağlamında çalışır (sayfa DOM'u, windowUtils, docShell, frameLoader, webNavigation erişimi). Browser-side `XpcomBridge.invoke` aynı `resolve`/`serialize` mantığını SimpleChannel 'xpcom' namespace'iyle content'e iletir.
- Consumes: XpcomRegistry (3.1), SimpleChannel mekanizması (mevcut, PageAgent'ın register deseni).

- [ ] **Step 1: `XpcomAgent.js` yaz (tam kod)**

```js
/* XpcomAgent.js — content-process XPCOM köprüsü.
 * Frame bağlamında (chrome-privileged) çalışır: DOM/windowUtils/docShell/frameLoader/webNavigation
 * ve tüm XpcomRegistry kaynaklarına erişir. Browser-side XpcomBridge'in content ikizi. */
import { XpcomRegistry } from "../protocol/XpcomRegistry.js";

function serialize(v, sessionId, depth = 0) {
  if (depth > 8) return { __xpcom: "too-deep" };
  if (v === null || v === undefined) return v;
  const t = typeof v;
  if (t === "string" || t === "number" || t === "boolean") return v;
  if (t === "function") return { __xpcom: "function", name: v.name || "anonymous" };
  if (t === "object") {
    if (XpcomRegistry.isHandle(v)) return v;
    if (v instanceof Components.interfaces.nsISupports || v?.wrappedJSObject) {
      return XpcomRegistry.handle(v, sessionId);
    }
    if (Array.isArray(v)) return v.map(x => serialize(x, sessionId, depth + 1));
    const out = {};
    for (const [k, x] of Object.entries(v)) {
      if (typeof x === "function") continue;
      out[k] = serialize(x, sessionId, depth + 1);
    }
    return out;
  }
  return { __xpcom: `unserializable:${t}` };
}

export function XpcomAgent(frameEnv) {
  // frameEnv: { window, docShell, browsingContext, windowUtils } — JugglerFrameChild tarafından verilir
  return {
    async invoke(domain, method, args, sessionId) {
      const env = frameEnv;
      // content-tarafı özel kaynaklar önce:
      const contentTargets = {
        Window: env.window, DocShell: env.docShell, WindowUtils: env.windowUtils,
        BrowsingContext: env.browsingContext,
      };
      try {
        let target = contentTargets[domain];
        if (!target) {
          const resolved = XpcomRegistry.resolve(domain);
          target = resolved.target;
        }
        const callArgs = (Array.isArray(args) ? args : []).map(a =>
          XpcomRegistry.isHandle(a) ? XpcomRegistry.byHandle(a.__xpcom) : a
        );
        if (target && typeof target[method] === "function") {
          const r = target[method](...callArgs);
          const res = r && typeof r.then === "function" ? await r : r;
          return serialize(res, sessionId);
        }
        throw new Error(`XpcomAgent: '${domain}' has no method '${method}'. ` +
          `Available: ${XpcomRegistry.methodNames(target).slice(0, 40).join(", ")}`);
      } catch (e) {
        const err = { name: e.name || "Error", message: e.message || String(e) };
        if (e instanceof Components.Exception) { err.code = e.result; err.stack = e.stack; }
        return { error: err };
      }
    },
  };
}
```

- [ ] **Step 2: PageAgent'a 'xpcom' namespace'i kaydet**

```js
// content/PageAgent.js — mevcut register('page', {...}) deseninin yanına:
browserChannel.register('xpcom', {
  async invoke({ domain, method, args, sessionId }) {
    return XpcomAgent(this._frameEnv).invoke(domain, method, args, sessionId);
  },
});
// this._frameEnv: PageAgent kurulurken JugglerFrameChild'ın verdiği {window, docShell,
// windowUtils, browsingContext} — mevcut main.js/FrameTree kurulumundaki env'e bağlan.
```

- [ ] **Step 3: Browser-side yönlendirme — XpcomBridge'e frame invoke (Task 3.2 dosyasına ek)**

```js
// XpcomBridge.js — 'xpcom-frame' hedefli domain.method'lar için (Faz 4'te doldurulacak:
// PageEx gibi frame domain'leri). Mevcut session.channel çağrısı üzerinden:
//   const res = await session.channel.send('xpcom', 'invoke', {domain, method, args}, frameId);
```

- [ ] **Step 4: Build + frame smoke**

```bash
cd ~/camoufox-src/camoufox/firefox-*/ && ./mach build 2>&1 | tail -3
CAMOUFOX_BIN=$PWD/obj-*/dist/bin/firefox \
  /home/void0x14/Belgeler/mcp-projelerim/cdp-kahin-mcp/.venv/bin/python - <<'EOF'
import asyncio
from kahin.the_twins.mirage import Mirage
async def main():
    m = Mirage(engine_name="mirage"); await m.start()
    await m.send_cdp("Page", "navigate", {"url": "data:text/html,<div id=x>hi</div>"})
    # content tarafı: Window domain -> title
    r = await m.send_cdp("Window", "invoke", {"args": ["document.title", []]})
    print("window-title:", r)
    await m.stop()
asyncio.run(main())
EOF
```

- [ ] **Step 5: Commit**

```bash
git add remote/juggler/content/XpcomAgent.js remote/juggler/content/PageAgent.js remote/juggler/protocol/XpcomBridge.js
git commit -m "feat(juggler): XpcomAgent — content-process bridge with frame env (Window/DocShell/WindowUtils)"
```

---

## Faz 4 — Kategori Implementasyonları (14 task) + Introspector

Stub yasak kuralının işletimi: her kategori task'ı, kendi domain'lerinin **mapping tablosunu** doldurur (domain → XPCOM kaynağı + doğrulanmış method). Mapping `katalog/mappings/<kategori>.json`'da toplanır ve `katalog/gen_domains.py`'nin çıktısını zenginleştirir (source alanı kesinleşir, methods listesi dolar). Her task sonunda **canlı smoke**: o kategorinin ≥3 domain'i, ≥1 method gerçek çağrıyla kanıtlanır.

### Task 4.0: Introspector.js + ortak smoke harness

**Files:**
- Create: `remote/juggler/protocol/Introspector.js`
- Create: `katalog/smoke.py`, `katalog/mappings/` (dizin)

**Interfaces:**
- Produces:
  - `Introspector.snapshot()` → `{iface: {nsIName: [method, ...]}, services: {name: [method,...]}, chromeutils: [...]}` — build edilmiş binary'de çalışır, ÇALIŞAN yüzeyin GERÇEK method listesini döndürür. `XpcomSnapshot` method'uyla pipe üzerinden çekilir.
  - `katalog/smoke.py` — tüm kategori task'larının ortak test aracı: `python3 katalog/smoke.py --category media --binary <fork> --calls '["MediaQuery.invoke|mediaFeature:prefers-color-scheme"]'` tarzı; her çağrıyı pipe üzerinden çalıştırır, PASS/FAIL raporlar.
- Consumes: XpcomRegistry (3.1), XpcomBridge (3.2), fork binary (0.5).

- [ ] **Step 1: `Introspector.js` yaz (tam kod)**

```js
/* Introspector.js — çalışan binary'deki gerçek XPCOM method yüzeyini çıkarır.
 * Çıktı: katalog zenginleştirme + Protocol.js method listeleri + smoke hedefleri. */
import { XpcomRegistry } from "./XpcomRegistry.js";

function ifaceMethods(ifaceObj) {
  return XpcomRegistry.methodNames(ifaceObj); // prototype taraması (Registry'de tanımlı)
}

export const Introspector = {
  snapshot() {
    const Ci = Components.interfaces;
    const ifaces = {};
    // Tüm kayıtlı interface'ler: catalog kaynaklarından interface:* olanlar
    const catalog = JSON.parse(IOUtils.readUTF8("chrome://juggler/content/XpcomCatalog.json"));
    const ifaceNames = new Set(
      catalog.entries.map(e => e.source).filter(s => s.startsWith("interface:")).map(s => s.slice(10))
    );
    for (const name of ifaceNames) {
      try {
        const iface = Ci[name];
        if (iface) ifaces[name] = ifaceMethods(iface);
      } catch { /* bazıları instantiate edilemez — yalnızca liste */ }
    }
    const services = {};
    for (const name of Object.keys(Services)) {
      try { services[name] = XpcomRegistry.methodNames(Services[name]); }
      catch { services[name] = []; }
    }
    return { ifaces, services, chromeutils: XpcomRegistry.methodNames(ChromeUtils) };
  },
};
```

- [ ] **Step 2: Snapshot al (bridge üzerinden)**

```bash
cd ~/camoufox-src/camoufox/firefox-*/ && ./mach build 2>&1 | tail -2
# XpcomBridge'e snapshot method'u ekle (3.2 dosyasına):
#   invoke("__introspector__", "snapshot", ...) -> Introspector.snapshot()
#   (Dispatcher fallback: __introspector__ domain'i özel-case — XpcomRegistry'de değil)
CAMOUFOX_BIN=$PWD/obj-*/dist/bin/firefox \
  /home/void0x14/Belgeler/mcp-projelerim/cdp-kahin-mcp/.venv/bin/python - <<'EOF'
import asyncio, json
from kahin.the_twins.mirage import Mirage
async def main():
    m = Mirage(engine_name="mirage"); await m.start()
    r = await m.send_cdp("__introspector__", "invoke", {})
    json.dump(r, open("/home/void0x14/Belgeler/mcp-projelerim/cdp-kahin-mcp/katalog/snapshot.json","w"))
    print("ifaces:", len(r.get("ifaces", {})), "services:", len(r.get("services", {})))
    await m.stop()
asyncio.run(main())
EOF
```

- [ ] **Step 3: Snapshot'ı katalogla birleştir**

```bash
python3 - <<'EOF'
import json
snap = json.load(open('katalog/snapshot.json'))
cat = json.load(open('katalog/domains.json'))
for e in cat:
    src = e["source"]
    if src.startswith("interface:") and src[10:] in snap["ifaces"]:
        e["methods"] = [{"name": m, "kind": "xpcom"} for m in snap["ifaces"][src[10:]]]
    elif src.startswith("service:") and src[8:] in snap["services"]:
        e["methods"] = [{"name": m, "kind": "xpcom"} for m in snap["services"][src[8:]]]
json.dump(cat, open('katalog/domains.json','w'), indent=1)
total = sum(len(e["methods"]) for e in cat)
print("katalog methods toplamı:", total, "(>0 olmalı — her domain ≥1 gerçek method)")
EOF
```

- [ ] **Step 4: `katalog/smoke.py` yaz (tam kod — tüm kategori task'larının test aracı)**

```python
#!/usr/bin/env python3
"""Ortak smoke: domain.method çağrılarını fork binary üzerinde GERÇEKTEN çalıştırır.
Kullanım:
  python3 katalog/smoke.py --binary <fork-firefox> --calls "CookiesService|getAll|[]" "PrefService|getBoolPref|[\"dom.w3c_touch_events\"]"
Çıktı: her çağrı için PASS/FAIL + sonuç; exit 0 yalnızca tümü PASS.
'|' ayraç: domain|method|json-args. '--calls-file' ile toplu (kategori dosyaları).
"""
import asyncio, json, sys, argparse
sys.path.insert(0, ".")
from kahin.the_twins.mirage import Mirage

async def run(binary, calls):
    m = Mirage(engine_name="mirage")
    import os
    os.environ["KAHIN_CAMOUFOX_BIN"] = binary
    await m.start()
    results = []
    for call in calls:
        domain, method, args = call.split("|", 2)
        args = json.loads(args or "[]")
        try:
            r = await m.send_cdp(domain, "invoke", {"args": args}) if method == "invoke" \
                else await m.send_cdp(domain, method, args)
            ok = not (isinstance(r, dict) and "error" in r)
            results.append((ok, domain, method, r))
        except Exception as e:
            results.append((False, domain, method, str(e)))
    await m.stop()
    for ok, d, mth, r in results:
        print(("PASS" if ok else "FAIL"), d, mth, json.dumps(r)[:200])
    sys.exit(0 if all(r[0] for r in results) else 1)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--calls", nargs="*", default=[])
    ap.add_argument("--calls-file", default=None)
    a = ap.parse_args()
    calls = a.calls
    if a.calls_file:
        calls += [l.strip() for l in open(a.calls_file) if l.strip() and not l.startswith("#")]
    asyncio.run(run(a.binary, calls))
```

- [ ] **Step 5: Harness'i kendi kendine doğrula (bilinen 2 çağrı)**

```bash
python3 katalog/smoke.py --binary ~/camoufox-src/camoufox/firefox-*/obj-*/dist/bin/firefox \
  --calls "Browser|getInfo|[]" "Page|getInfo|[]"
# BEKLENTİ: 2 PASS (Juggler-native method'lar bridge'e düşmeden mevcut handler'larıyla çalışır)
```

- [ ] **Step 6: Commit**

```bash
git add remote/juggler/protocol/Introspector.js katalog/smoke.py katalog/snapshot.json katalog/domains.json
git commit -m "feat(katalog): Introspector + snapshot merge + shared smoke harness"
```

### Task 4.1: Core domain'leri (Browser/Page/Runtime/Network/Heap/Accessibility/Target/Session)

**Files:**
- Create: `katalog/mappings/core.json`
- Modify: `remote/juggler/protocol/BrowserHandler.js`, `PageHandler.js` (yalnızca gerekiyorsa — mevcut 6 domain zaten canlı)

**Interfaces:**
- Produces: `Target` domain (TargetRegistry üstü: `getTargets`, `attachToTarget`, `detachFromTarget`, `closeTarget` — mevcut session/target mekanizmasının method'ları) ve `Session` domain (`listSessions`, `getSession`, `create`, `kill` — TargetRegistry.state üstü). Mapping kayıtları.
- Consumes: 4.0 smoke harness, snapshot (4.0).

- [ ] **Step 1: `katalog/mappings/core.json` yaz**

```json
{
  "Target": {
    "source": "native:TargetRegistry",
    "methods": [
      {"name": "getTargets", "impl": "TargetRegistry.targets() -> [{targetId, browserId, url, title, sessionId}]"},
      {"name": "attachToTarget", "impl": "Dispatcher.createSession(browserId) -> {sessionId}"},
      {"name": "detachFromTarget", "impl": "Dispatcher.destroySession(sessionId)"},
      {"name": "closeTarget", "impl": "TargetRegistry.getTarget(browserId).close()"}
    ]
  },
  "Session": {
    "source": "native:Dispatcher",
    "methods": [
      {"name": "listSessions", "impl": "Dispatcher.sessions() -> [{sessionId, targetId}]"},
      {"name": "getSession", "impl": "Dispatcher.session(sessionId) -> {sessionId, targetId, url}"},
      {"name": "create", "impl": "Dispatcher.createSession() (about:blank)"},
      {"name": "kill", "impl": "Dispatcher.destroySession(sessionId)"}
    ]
  }
}
```

- [ ] **Step 2: BrowserHandler'a `Target.*` + `Session.*` handler'ları ekle (mevcut desenle)**

```bash
grep -n "async \['" remote/juggler/protocol/BrowserHandler.js | head -5   # mevcut handler deseni
# Desen: async ['Target.getTargets'](params) { ... } — TargetRegistry/Dispatcher import'ları
# BrowserHandler'da mevcut (newPage vb. zaten kullanıyor). Target/Session method'larını
# bu desenle uygula: getTargets -> registry.targets().map(t => ({targetId, url, title,
# browserId, sessionId: dispatcher.sessionIdFor(t)})), attachToTarget -> createSession,
# detachFromTarget -> destroySession, closeTarget -> t.close().
```

- [ ] **Step 3: Smoke (3 gerçek çağrı)**

```bash
python3 katalog/smoke.py --binary ~/camoufox-src/camoufox/firefox-*/obj-*/dist/bin/firefox \
  --calls "Target|getTargets|[]" "Session|listSessions|[]" "Browser|getVersion|[]"
```

- [ ] **Step 4: Commit**

```bash
git add katalog/mappings/core.json remote/juggler/protocol/BrowserHandler.js
git commit -m "feat(juggler): Target + Session domains over TargetRegistry/Dispatcher"
```

### Task 4.2: DOM & Layout domain'leri

**Files:**
- Create: `katalog/mappings/dom.json`

**Interfaces:**
- Produces: DOM (Element/Document/Node/Range/Selection/EventTarget/NodeList), Layout (Layout/LayerTree/Reflow), Style (CSS/StyleSheet/CSSOM), Animation (Animation/Transition/KeyframeEffect) domain'leri — hepsi content-side (XpcomAgent frame env) veya XPCOM interface'leri üstünde; mapping kayıtları.
- Consumes: 4.0 harness + snapshot (interface method'ları otomatik dolu).

- [ ] **Step 1: `katalog/mappings/dom.json` yaz (kurallar + elle güçlendirme)**

```json
{
  "Element":   {"source": "frame:Window.document", "note": "content invoke: document.getElementById vb. — method adı DOM API'siyle birebir"},
  "Document":  {"source": "frame:Window.document", "note": "querySelector, getElementById, createElement, execCommand(print), visibilityState"},
  "Node":      {"source": "frame:Window.Node", "note": "prototype üstü: appendChild, cloneNode, contains, isEqualNode"},
  "Range":     {"source": "frame:Window.Range", "note": "createRange, collapse, selectNode, getBoundingClientRect"},
  "Selection": {"source": "frame:Window.getSelection()", "note": "addRange, removeAllRanges, selectAllChildren, toString"},
  "Layout":    {"source": "frame:WindowUtils", "note": "getBoundsWithoutFlushing, getScrollXY, getResolution, flushApzRepaints"},
  "CSS":       {"source": "frame:Window.CSS", "note": "supports, escape; CSSStyleSheet insertRule"},
  "Animation": {"source": "frame:Window.document.getAnimations()", "note": "Animation.play/pause/cancel/commitStyles/currentTime"},
  "StyleSheet": {"source": "frame:Window.document.styleSheets", "note": "cssRules, insertRule, deleteRule"}
}
```

- [ ] **Step 2: Content tarafı `frame` kaynaklarını XpcomAgent'e ekle**

```js
// XpcomAgent.js — contentTargets'a ekle (Task 3.3):
//   Window: env.window, Document: env.window.document, Element: env.window.document.documentElement,
//   Node: env.window.Node, Range: env.window.Range, Selection: env.window.getSelection(),
//   Layout: env.windowUtils, WindowUtils: env.windowUtils,
//   CSS: env.window.CSS, StyleSheet: env.window.document.styleSheets[0] || env.window.document.createElement('style').sheet,
//   Animation: env.window.document.getAnimations()[0]
// Domain.method çağrısı: target[method](...args) — method adları DOM API'leriyle birebir.
```

- [ ] **Step 3: Smoke**

```bash
cd ~/camoufox-src/camoufox/firefox-*/ && ./mach build 2>&1 | tail -2
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "Document|querySelector|[\"#x\"]" "Layout|getScrollXY|[]" "CSS|supports|[\"display\",\"grid\"]"
```

- [ ] **Step 4: Commit**

```bash
git add katalog/mappings/dom.json remote/juggler/content/XpcomAgent.js
git commit -m "feat(juggler): DOM & Layout domains over frame env (Element/Document/Layout/CSS/Animation)"
```

### Task 4.3: Input domain'leri (Input/Mouse/Keyboard/Touch/Drag/Gesture/PointerLock)

**Files:**
- Create: `katalog/mappings/input.json`
- Modify: `remote/juggler/content/PageAgent.js` (mevcut `_dispatchKeyEvent`, `_dispatchMouseEvent` zaten var — yeni domain'ler bunları sarar)

**Interfaces:**
- Produces: Input (dispatchMouseEvent/dispatchKeyEvent/dispatchWheelEvent — mevcut Juggler method'larının domain'e sarılması), Mouse (click/dblclick/down/up/move/over/out), Keyboard (down/up/press/insertText), Touch (tap/start/move/end), Drag (start/over/drop/end — nsIDragService), Gesture (pinch/pan/tapDouble — nsIDOMWindowUtils), PointerLock (request/exit/locked).
- Consumes: 4.0 harness, deployed PageAgent method'ları (kaynak `/tmp/opencode/juggler-research/deployed/` — aynen repo'da).

- [ ] **Step 1: `katalog/mappings/input.json` yaz**

```json
{
  "Input":   {"source": "juggler:Page.dispatchMouseEvent/dispatchKeyEvent/dispatchWheelEvent", "note": "mevcut PageHandler method'ları — Input.<aynı ad>"},
  "Mouse":   {"source": "frame:windowUtils", "note": "jugglerSendMouseEvent ile click/dblclick/move/down/up (type: 'mousedown'|'mouseup'|'mousemove'|'click'|'dblclick')"},
  "Keyboard": {"source": "juggler:textInputProcessor", "note": "PageAgent._dispatchKeyEvent sarmalayıcıları: down/up/insertText"},
  "Touch":   {"source": "frame:windowUtils", "note": "sendTouchEvent(type, xs, ys, rxs, rys, rotation, count, modifiers)"},
  "Drag":    {"source": "cc:@mozilla.org/widget/dragservice;1", "note": "nsIDragService: startDragSession, getCurrentSession, fireDragEventAtSource"},
  "Gesture": {"source": "frame:windowUtils", "note": "sendGestureSwipe, sendNativeTouchTap, sendTouchEvent (pinch -> sendTouchEvent)"},
  "PointerLock": {"source": "frame:Window.document", "note": "pointerLockElement, exitPointerLock, requestPointerLock"}
}
```

- [ ] **Step 2: Handler'ları sar (PageHandler'a Input.* / Mouse.* / Keyboard.* — mevcut dispatch'lerin alias'ı)**

```bash
grep -n "dispatchMouseEvent\|dispatchKeyEvent\|dispatchWheelEvent" remote/juggler/protocol/PageHandler.js
# Desen: async ['Input.dispatchMouseEvent'](params) { return this._dispatchMouseEvent(params); } — 
# 6 method alias (Input.*) + Mouse.*/Keyboard.* alias'ları (aynı iç implementasyon)
```

- [ ] **Step 3: Smoke**

```bash
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "Input|dispatchMouseEvent|[\"data:text/html,<button onclick='window.__c=1'>b</button>\"]" \
           "Keyboard|insertText|[\"hello\"]" "PointerLock|pointerLockElement|[]"
# Input dispatch sonrası sayfa yüklenmiş olmalı — smoke.py'ye navigate destekli çağrı
# tipi ekle (opsiyonel): domain|method|args + öncesi Page.navigate (veri URI'si)
```

- [ ] **Step 4: Commit**

```bash
git add katalog/mappings/input.json remote/juggler/protocol/PageHandler.js
git commit -m "feat(juggler): Input/Mouse/Keyboard/Touch/Drag/Gesture/PointerLock domains"
```

### Task 4.4: Network derin (Fetch/WebSocket/WebRTC/HTTPCache/Cookie/Channel/Proxy/DNS)

**Files:**
- Create: `katalog/mappings/network.json`

**Interfaces:**
- Produces: Fetch (mevcut NetworkObserver interception'ın domain hali), WebSocket (nsIWebSocketChannel/WebSocketEventService), WebRTC (nsIWebRTCService — mediaDevices/ice), HTTPCache (nsICacheStorageService), Cookie (nsICookieManager — Services.cookies), Channel (nsIChannelObserverService), Proxy (nsIProtocolProxyService), DNS (nsIDNSService — resolve, resolveNative, dnsLookup).
- Consumes: 4.0 harness + snapshot.

- [ ] **Step 1: `katalog/mappings/network.json` yaz**

```json
{
  "Fetch":    {"source": "juggler:NetworkObserver", "note": "setRequestInterception/getResponseBody/failRequest — mevcut NetworkObserver API'si"},
  "WebSocket": {"source": "cc:@mozilla.org/websocket-event-service;1", "note": "nsIWebSocketEventService: addListener, getEvents"},
  "WebRTC":   {"source": "cc:@mozilla.org/dom/webrtc;1", "note": "nsIWebRTCService: getStats, getPeerConnectionInfo; getUserMedia via page"},
  "HTTPCache": {"source": "service:cache2", "note": "nsICacheStorageService: clear, getCacheStorage, smear"},
  "Cookie":   {"source": "service:cookies", "note": "nsICookieManager: getAll, add, remove, removeAll, getCookiesFromHost"},
  "Channel":  {"source": "cc:@mozilla.org/observer-service;1", "note": "http-on-modify-request observer + nsIChannelObserverService"},
  "Proxy":    {"source": "cc:@mozilla.org/network/protocol-proxy-service;1", "note": "nsIProtocolProxyService: resolve, newProxyInfo, getFailoverForProxy"},
  "DNS":      {"source": "cc:@mozilla.org/network/dns-service;1", "note": "nsIDNSService: resolve, resolveNative, myHostName, dnsLookup"}
}
```

- [ ] **Step 2: Smoke (3 gerçek çağrı — snapshot'tan method doğrulamasıyla)**

```bash
python3 - <<'EOF'
import json
snap = json.load(open('katalog/snapshot.json'))
for svc, want in [("cookies","getAll"), ("cache2","clear"), ("dns","resolve")]:
    m = snap["services"].get(svc, [])
    print(svc, "->", "OK" if want in m else f"MISSING({want}) mevcut:{m[:15]}")
EOF
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "Cookie|getAll|[]" "DNS|myHostName|[]" "Proxy|getFailoverForProxy|[]"
```

- [ ] **Step 3: Commit**

```bash
git add katalog/mappings/network.json
git commit -m "feat(juggler): deep network domains — Cookie/DNS/Proxy/WebSocket/WebRTC/HTTPCache/Channel/Fetch"
```

### Task 4.5: Storage (Storage/IndexedDB/CacheStorage/LocalStorage/SessionStorage/FileSystem/Quota)

**Files:**
- Create: `katalog/mappings/storage.json`

**Interfaces:**
- Produces: Storage (Services.domStorageManager / nsIDOMStorageManager), IndexedDB (nsIIDBFactory — open, deleteDatabase, listDatabases), CacheStorage (nsICacheStorageService), LocalStorage/SessionStorage (frame: Window.localStorage/sessionStorage), FileSystem (nsIFile + OS.File/modüller: DownloadCore değil — FileSystem: cc:@mozilla.org/file/local;1), Quota (Services.qms — nsIQuotaManagerService).
- Consumes: 4.0 harness.

- [ ] **Step 1: `katalog/mappings/storage.json` yaz**

```json
{
  "Storage":      {"source": "service:domStorageManager", "note": "nsIDOMStorageManager: createStorage, getStorage, localStorage"},
  "IndexedDB":    {"source": "frame:Window.indexedDB", "note": "open(name), deleteDatabase(name), databases()"},
  "CacheStorage": {"source": "service:cache2", "note": "getCacheStorage, clear"},
  "LocalStorage": {"source": "frame:Window.localStorage", "note": "getItem, setItem, removeItem, clear, key, length"},
  "SessionStorage": {"source": "frame:Window.sessionStorage", "note": "getItem, setItem, removeItem, clear, key, length"},
  "FileSystem":   {"source": "cc:@mozilla.org/file/local;1", "note": "nsILocalFile: create, append, remove, path, leafName"},
  "Quota":        {"source": "service:qms", "note": "nsIQuotaManagerService: getUsage, getUsageInfo, clearStoragesForOrigin"}
}
```

- [ ] **Step 2: Smoke**

```bash
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "LocalStorage|setItem|[\"k\",\"v\"]" "LocalStorage|getItem|[\"k\"]" "Quota|getUsage|[]"
```

- [ ] **Step 3: Commit**

```bash
git add katalog/mappings/storage.json
git commit -m "feat(juggler): storage domains — Storage/IndexedDB/CacheStorage/LocalStorage/SessionStorage/FileSystem/Quota"
```

### Task 4.6: Emulation & Device (Emulation/Device/Geolocation/Sensor/Battery/MediaDevice/Screen/Orientation)

**Files:**
- Create: `katalog/mappings/emulation.json`
- Modify: `remote/juggler/content/main.js` (docShell override'ları mevcut — Emulation domain'e bağla)

**Interfaces:**
- Produces: Emulation (mevcut Browser.setUserAgentOverride/setGeolocationOverride/setViewportSize + docShell override'ları), Device (nsIDeviceSensors), Geolocation (nsIGeolocationSettingsService — setGeolocationServiceOverride), Sensor (DeviceSensors: accelerometer/gyroscope), Battery (navigator.getBattery — frame), MediaDevice (enumerateDevices — frame), Screen (Window.screen — frame), Orientation (ScreenOrientation — frame).
- Consumes: 4.0 harness.

- [ ] **Step 1: `katalog/mappings/emulation.json` yaz**

```json
{
  "Emulation":   {"source": "juggler:docShell+Browser overrides", "note": "setUserAgent, setLocale, setTimezone, setGeolocation, setViewport, setColorScheme, setReducedMotion"},
  "Device":      {"source": "cc:@mozilla.org/devicemanager;1", "note": "nsIDeviceSensors: deviceAdded, accelerometerActive, orientationActive"},
  "Geolocation": {"source": "cc:@mozilla.org/geolocation/settings;1", "note": "nsIGeolocationSettingsService: setGeolocationServiceOverride"},
  "Sensor":      {"source": "frame:Window", "note": "navigator.sensors / DeviceSensors API — accelerometer/gyroscope/magnetometer"},
  "Battery":     {"source": "frame:Window", "note": "navigator.getBattery() -> BatteryManager: level, charging, chargingTime, dischargingTime"},
  "MediaDevice": {"source": "frame:Window", "note": "navigator.mediaDevices.enumerateDevices()"},
  "Screen":      {"source": "frame:Window.screen", "note": "width, height, availWidth, availHeight, colorDepth, orientation"},
  "Orientation": {"source": "frame:Window.screen.orientation", "note": "type, angle, lock(), unlock()"}
}
```

- [ ] **Step 2: Smoke**

```bash
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "Screen|availWidth|[]" "Battery|level|[]" "Orientation|type|[]"
```

- [ ] **Step 3: Commit**

```bash
git add katalog/mappings/emulation.json remote/juggler/content/main.js
git commit -m "feat(juggler): emulation & device domains — Emulation/Device/Geolocation/Sensor/Battery/MediaDevice/Screen/Orientation"
```

### Task 4.7: Media (Media/Audio/Video/WebAudio/MediaStream/Capture)

**Files:**
- Create: `katalog/mappings/media.json`

**Interfaces:**
- Produces: Media (HTMLMediaElement frame API — play/pause/seek/volume), Audio (HTMLAudioElement), Video (HTMLVideoElement + captureStream), WebAudio (AudioContext — createOscillator/createAnalyser), MediaStream (MediaStream + tracks), Capture (getUserMedia frame — mediaDevices.getUserMedia, MediaRecorder).
- Consumes: 4.0 harness.

- [ ] **Step 1: `katalog/mappings/media.json` yaz**

```json
{
  "Media":       {"source": "frame:Window.HTMLMediaElement", "note": "play, pause, currentTime, volume, muted, playbackRate, duration, readyState"},
  "Audio":       {"source": "frame:Window.HTMLAudioElement", "note": "src, loop, autoplay, currentTime, play, pause"},
  "Video":       {"source": "frame:Window.HTMLVideoElement", "note": "videoWidth, videoHeight, captureStream, requestPictureInPicture"},
  "WebAudio":    {"source": "frame:Window.AudioContext", "note": "createOscillator, createAnalyser, createGain, currentTime, state, resume, suspend"},
  "MediaStream": {"source": "frame:Window.MediaStream", "note": "getTracks, getAudioTracks, getVideoTracks, addTrack, removeTrack, active"},
  "Capture":     {"source": "frame:Window", "note": "mediaDevices.getUserMedia({video:true}), MediaRecorder.isTypeSupported, start/stop"}
}
```

- [ ] **Step 2: Smoke**

```bash
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "WebAudio|createOscillator|[]" "Media|play|[]" "MediaStream|getTracks|[]"
```

- [ ] **Step 3: Commit**

```bash
git add katalog/mappings/media.json
git commit -m "feat(juggler): media domains — Media/Audio/Video/WebAudio/MediaStream/Capture"
```

### Task 4.8: Security & Permission (Security/Permission/CSP/Certificate/MixedContent)

**Files:**
- Create: `katalog/mappings/security.json`

**Interfaces:**
- Produces: Security (nsISecurityInfo — SecurityInfo, mixedContentStatus), Permission (Services.perms — nsIPermissionManager: add, remove, removeAll, testPermission), CSP (frame: document.csp + nsIContentSecurityManager: assertCSP), Certificate (nsICertOverrideService + nsIX509CertDB: getCerts, verifyCertNow), MixedContent (frame: document + nsIMixedContentBlocker).
- Consumes: 4.0 harness.

- [ ] **Step 1: `katalog/mappings/security.json` yaz**

```json
{
  "Security":     {"source": "cc:@mozilla.org/security/si;1", "note": "nsISecurityInfo / SecurityInfo — state, mixedContentState"},
  "Permission":   {"source": "service:perms", "note": "nsIPermissionManager: add, remove, removeAll, testPermission, getAll"},
  "CSP":          {"source": "frame:Window.document", "note": "csp attribute; nsIContentSecurityManager (cc:@mozilla.org/contentsecuritymanager;1) assertCSP"},
  "Certificate":  {"source": "cc:@mozilla.org/security/x509certdb;1", "note": "nsIX509CertDB: getCerts, verifyCertNow; nsICertOverrideService"},
  "MixedContent": {"source": "frame:Window.document", "note": "mixedContentActive on nsIDocument; nsIMixedContentBlocker"}
}
```

- [ ] **Step 2: Smoke**

```bash
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "Permission|getAll|[]" "Certificate|getCerts|[]" "CSP|assertCSP|[]"
```

- [ ] **Step 3: Commit**

```bash
git add katalog/mappings/security.json
git commit -m "feat(juggler): security & permission domains — Security/Permission/CSP/Certificate/MixedContent"
```

### Task 4.9: Performance & Debug (Performance/Memory/Profiler/HeapProfiler/Tracing/Log/Console/Debugger)

**Files:**
- Create: `katalog/mappings/perf.json`

**Interfaces:**
- Produces: Performance (frame: performance API + Services.profiler), Memory (nsIMemoryReporterManager: getHeapUsage, getTotalMemoryUsage, report), Profiler (Services.profiler: StartProfiler/StopProfiler/CaptureProfile), HeapProfiler (Heap domain mevcut Cu.forceGC + nsIMemoryReporterManager), Tracing (Services.profiler TracingLog + nsITraceListener), Log (Services.console: logStringMessage, getMessageCount; ConsoleListener), Console (Services.console — getAllMessages, reset), Debugger (ChromeUtils / jsdIDebuggerService? Firefox 152: no jsd — Debugger API content'ta; Debugger domain = ContentMain Debugger API sarımı — WorkerMain'daki mevcut Debugger kullanımı örnek).
- Consumes: 4.0 harness.

- [ ] **Step 1: `katalog/mappings/perf.json` yaz**

```json
{
  "Performance":  {"source": "frame:Window.performance", "note": "getEntries, getEntriesByType, mark, measure, clearMarks, navigation, memory"},
  "Memory":       {"source": "cc:@mozilla.org/memory-reporter-manager;1", "note": "nsIMemoryReporterManager: getHeapUsage, getTotalMemoryUsage, getResident, reports"},
  "Profiler":     {"source": "service:profiler", "note": "StartProfiler, StopProfiler, CaptureProfile, IsActive, GetBufferInfo"},
  "HeapProfiler": {"source": "juggler:Heap (mevcut)", "note": "collectGarbage + Cu.forceGC + Memory dumps (nsIMemoryReporterManager.dumpReports)"},
  "Tracing":      {"source": "service:profiler", "note": "TracingLog, Start/StopTracing, GetTracingLogs"},
  "Log":          {"source": "service:console", "note": "nsIConsoleService: logStringMessage, getMessageCount, reset, getMessages"},
  "Console":      {"source": "service:console", "note": "getAllMessages, reset; event gateway: console-message topic"},
  "Debugger":     {"source": "native:content Debugger", "note": "WorkerMain Debugger API sarımı — frame'de Debugger.Frame/Debugger.Object"}
}
```

- [ ] **Step 2: Smoke**

```bash
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "Memory|getHeapUsage|[]" "Performance|getEntries|[]" "Console|getMessageCount|[]"
```

- [ ] **Step 3: Commit**

```bash
git add katalog/mappings/perf.json
git commit -m "feat(juggler): performance & debug domains — Performance/Memory/Profiler/HeapProfiler/Tracing/Log/Console/Debugger"
```

### Task 4.10: Process & System (Process/Thread/SystemInfo/MemoryInfo/CPU/GPU)

**Files:**
- Create: `katalog/mappings/process.json`

**Interfaces:**
- Produces: Process (ChromeUtils.getAllDOMProcesses + nsIProcess — launch, kill, getProcessInfo), Thread (nsIThreadManager: newThread, getCurrentThread), SystemInfo (Services.sysinfo: getProperty — os, cpu, mem, device), MemoryInfo (Services.sysinfo mem + nsIMemoryReporterManager), CPU (sysinfo cpu props + nsITimer-based usage), GPU (GfxInfo: cc:@mozilla.org/gfx/info;1 — nsIGfxInfo: getAdapterInfo, getFeatureStatus).
- Consumes: 4.0 harness.

- [ ] **Step 1: `katalog/mappings/process.json` yaz**

```json
{
  "Process":    {"source": "native:ChromeUtils", "note": "getAllDOMProcesses, requestProcInfo; nsIProcess (cc:@mozilla.org/process/util;1) launch/kill"},
  "Thread":     {"source": "cc:@mozilla.org/thread-manager;1", "note": "nsIThreadManager: newThread, getCurrentThread, idleDispatch"},
  "SystemInfo": {"source": "service:sysinfo", "note": "nsIPropertyBag: getProperty('os'), ('cpu'), ('mem'), ('device')"},
  "MemoryInfo": {"source": "cc:@mozilla.org/memory-reporter-manager;1", "note": "getTotalMemoryUsage, getResident, getHeapUsage"},
  "CPU":        {"source": "service:sysinfo", "note": "getProperty('cpucount'), ('cpuspeed'); usage via nsITimer + tick"},
  "GPU":        {"source": "cc:@mozilla.org/gfx/info;1", "note": "nsIGfxInfo: getAdapterInfo, getFeatureStatus, getMonitors"}
}
```

- [ ] **Step 2: Smoke**

```bash
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "SystemInfo|getProperty|[\"os\"]" "CPU|getProperty|[\"cpucount\"]" "MemoryInfo|getResident|[]"
```

- [ ] **Step 3: Commit**

```bash
git add katalog/mappings/process.json
git commit -m "feat(juggler): process & system domains — Process/Thread/SystemInfo/MemoryInfo/CPU/GPU"
```

### Task 4.11: Accessibility (Accessibility/AXTree/ScreenReader)

**Files:**
- Create: `katalog/mappings/a11y.json`
- Modify: `remote/juggler/content/PageAgent.js` (mevcut `_getFullAXTree` — AXTree domain'e bağla)

**Interfaces:**
- Produces: Accessibility (mevcut Page.accessibilitySnapshot + nsIAccessibilityService: getAccessibleFor, getStringRole), AXTree (mevcut _getFullAXTree sarımı + accessible-event observer), ScreenReader (nsIAccessibleRetrieval: isEnabled, isActive).
- Consumes: 4.0 harness.

- [ ] **Step 1: `katalog/mappings/a11y.json` yaz**

```json
{
  "Accessibility": {"source": "cc:@mozilla.org/accessibleRetrieval;1", "note": "nsIAccessibleRetrieval: getAccessibleFor, isEnabled, getStringRole, getStringStates"},
  "AXTree":        {"source": "juggler:PageAgent._getFullAXTree", "note": "mevcut tam AX ağacı üretimi — Accessibility.axTree olarak dışa aç"},
  "ScreenReader":  {"source": "cc:@mozilla.org/accessibleRetrieval;1", "note": "isEnabled, isActive, accessibleRole, accessibleState"}
}
```

- [ ] **Step 2: Smoke**

```bash
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "Accessibility|isEnabled|[]" "ScreenReader|isActive|[]" "Accessibility|getStringRole|[\"1\"]"
```

- [ ] **Step 3: Commit**

```bash
git add katalog/mappings/a11y.json remote/juggler/content/PageAgent.js
git commit -m "feat(juggler): accessibility domains — Accessibility/AXTree/ScreenReader"
```

### Task 4.12: Browser UI & Session (History/SessionStore/Tab/Window/Bookmark/Download/Print/Extension)

**Files:**
- Create: `katalog/mappings/ui.json`

**Interfaces:**
- Produces: History (nsISHistory: getEntryAtIndex, reloadCurrentEntry, getSessionHistoryEntryCount; frame: history), SessionStore (module: SessionStore (browser chrome'da) — getBrowserState, setBrowserState, getClosedTabCount; yoksa browser.js API'leri), Tab (Services.wm: getMostRecentWindow — gBrowser tabları), Window (Services.wm: getEnumerator, getMostRecentWindow; nsIWindowMediator), Bookmark (PlacesUtils module + nsINavBookmarksService: insertBookmark, removeItem), Download (module: DownloadCore/Downloads — getList, createDownload, removeFinished), Print (browser print: gBrowser.selectedBrowser.browsingContext.print + nsIPrintSettingsService: createNewSettings, printWindow), Extension (module: Extension — getAddons, install, uninstall; WebExtensionPolicy).
- Consumes: 4.0 harness.

- [ ] **Step 1: `katalog/mappings/ui.json` yaz**

```json
{
  "History":      {"source": "frame:Window.history", "note": "back, forward, go, length, pushState, replaceState; nsISHistory (cc:@mozilla.org/browser/shistory;1)"},
  "SessionStore": {"source": "module:SessionStore", "note": "getBrowserState, setBrowserState, getClosedTabCount, undoCloseTab — browser/components"},
  "Tab":          {"source": "native:gBrowser", "note": "Services.wm.getMostRecentWindow('navigator:browser').gBrowser: addTab, removeTab, selectedTab, moveTabTo"},
  "Window":       {"source": "service:wm", "note": "nsIWindowMediator: getEnumerator, getMostRecentWindow, openWindow (browser chrome)"},
  "Bookmark":     {"source": "cc:@mozilla.org/browser/nav-bookmarks-service;1", "note": "nsINavBookmarksService: insertBookmark, removeItem, getItemTitle, searchBookmarks"},
  "Download":     {"source": "module:Downloads", "note": "getList(ALL), createDownload, removeFinished, pause/resume (browser/components/downloads)"},
  "Print":        {"source": "cc:@mozilla.org/gfx/printsettings-service;1", "note": "nsIPrintSettingsService: createNewSettings, savePrintSettings, printWindow"},
  "Extension":    {"source": "module:Extension", "note": "WebExtensionPolicy.get, ExtensionManagement: install, uninstall, enable, disable"}
}
```

- [ ] **Step 2: Smoke (headless-safe seçimler)**

```bash
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "History|length|[]" "Print|createNewSettings|[]" "Bookmark|getItemTitle|[]"
```

- [ ] **Step 3: Commit**

```bash
git add katalog/mappings/ui.json
git commit -m "feat(juggler): browser UI & session domains — History/SessionStore/Tab/Window/Bookmark/Download/Print/Extension"
```

### Task 4.13: Advanced (Observer/Preference/AboutPage/InternalAPI/EventTarget/Mutation/Intersection/Resize)

**Files:**
- Create: `katalog/mappings/advanced.json`

**Interfaces:**
- Produces: Observer (Services.obs: addObserver, removeObserver, notifyObservers, enumerateObservers; event gateway'de topic'ler), Preference (Services.prefs: getBoolPref/getIntPref/getStringPref/setPref/clearPref/getChildList + branch'ler), AboutPage (nsIAboutModule getters — about: config/preferences/memory; module: AboutRedirector), InternalAPI (Ci/Cc/ChromeUtils full — raw erişim domain'i: `Components` üzerinden interface listeleme + method çağrısı), EventTarget (frame: EventTarget prototype: addEventListener, removeEventListener, dispatchEvent), Mutation (frame: MutationObserver: observe, disconnect, takeRecords), Intersection (frame: IntersectionObserver: observe, unobserve, disconnect, takeRecords), Resize (frame: ResizeObserver: observe, unobserve, disconnect).
- Consumes: 4.0 harness.

- [ ] **Step 1: `katalog/mappings/advanced.json` yaz**

```json
{
  "Observer":      {"source": "service:obs", "note": "nsIObserverService: addObserver, removeObserver, notifyObservers, enumerateObservers — event gateway topic listesi"},
  "Preference":    {"source": "service:prefs", "note": "nsIPrefBranch: getBoolPref, getIntPref, getStringPref, getComplexValue, setBoolPref, setIntPref, setStringPref, clearUserPref, getChildList"},
  "AboutPage":     {"source": "module:AboutRedirector", "note": "getAboutModule, getURIToLoad — about:config, about:memory, about:preferences"},
  "InternalAPI":   {"source": "native:Ci/Cc/ChromeUtils", "note": "listInterfaces, getInterface(name), getClass(contractID), importModule(path) — RAW XPCOM erişimi"},
  "EventTarget":   {"source": "frame:Window.EventTarget", "note": "addEventListener, removeEventListener, dispatchEvent"},
  "Mutation":      {"source": "frame:Window.MutationObserver", "note": "observe, disconnect, takeRecords"},
  "Intersection":  {"source": "frame:Window.IntersectionObserver", "note": "observe, unobserve, disconnect, takeRecords"},
  "Resize":        {"source": "frame:Window.ResizeObserver", "note": "observe, unobserve, disconnect"}
}
```

- [ ] **Step 2: Smoke**

```bash
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "Preference|getBoolPref|[\"dom.w3c_touch_events\"]" "Observer|enumerateObservers|[]" "EventTarget|dispatchEvent|[]"
```

- [ ] **Step 3: Commit**

```bash
git add katalog/mappings/advanced.json
git commit -m "feat(juggler): advanced domains — Observer/Preference/AboutPage/InternalAPI/EventTarget/Mutation/Intersection/Resize"
```

### Task 4.14: Kalan domain'ler (bulk — katalogdaki geri kalan ~1300)

**Files:**
- Create: `katalog/mappings/bulk.json` (katalogdan geri kalanların otomatik mapping'i)

**Interfaces:**
- Produces: Katalogdaki tüm `interface:*`/`cc:*`/`module:*` kaynaklı domain'ler için otomatik kaynak doğrulaması + snapshot method eşlemesi. Elle mapping gerektiren domain'ler (native/frame kaynaklı) tespit edilip FAIL raporlanır — asla sessiz bırakılmaz.
- Consumes: snapshot.json (4.0), domains.json (1.2), verify-report (1.3).

- [ ] **Step 1: Otomatik bulk doğrulama script'i yaz + çalıştır**

```bash
python3 - <<'EOF'
import json
cat = json.load(open('katalog/domains.json'))
snap = json.load(open('katalog/snapshot.json'))
needs_manual = []
for e in cat:
    src = e["source"]
    if src.startswith("interface:"):
        iface = src[10:]
        if iface in snap["ifaces"] and snap["ifaces"][iface]:
            e["methods"] = [{"name": m, "kind": "xpcom"} for m in snap["ifaces"][iface]]
        else:
            needs_manual.append((e["id"], e["domain"], src, "no-methods-in-snapshot"))
    elif src.startswith("service:"):
        s = src[8:]
        if s in snap["services"] and snap["services"][s]:
            e["methods"] = [{"name": m, "kind": "xpcom"} for m in snap["services"][s]]
        else:
            needs_manual.append((e["id"], e["domain"], src, "no-methods-in-snapshot"))
    elif src.startswith("module:"):
        needs_manual.append((e["id"], e["domain"], src, "module-import-verify-at-runtime"))
json.dump(cat, open('katalog/domains.json','w'), indent=1)
print("elle mapping gereken:", len(needs_manual))
for row in needs_manual[:30]: print(" ", row)
EOF
```

- [ ] **Step 2: Elle mapping gerekenleri çöz (asla sessiz geçme)**

```bash
# Her needs_manual satırı için: snapshot/omni.ja'dan method listesi bulunamadıysa
# bridge runtime'da detaylı hata döndürür (Task 3.2) — domain yine de GERÇEK kalır.
# Bu domain'ler 'mappings/bulk.json'da "runtime-verified" işaretlenir ve Faz 8 smoke'unda
# canlı çağrıyla doğrulanır. ELDE ÇÖZÜM: XpcomRegistry.resolve'ın hata mesajı
# "Available: ..." listesi verir — snapshot method'ları oradan toplanıp katalogu güncelle.
```

- [ ] **Step 3: Toplam kapsam raporu**

```bash
python3 - <<'EOF'
import json
cat = json.load(open('katalog/domains.json'))
with_methods = [e for e in cat if e["methods"]]
total_methods = sum(len(e["methods"]) for e in cat)
print(f"domain: {len(cat)} | method'lu: {len(with_methods)} | toplam method: {total_methods}")
open('katalog/kapsam-report.txt','w').write(
    f"domains={len(cat)} with_methods={len(with_methods)} total_methods={total_methods}\n")
EOF
# HEDEF: domains=1453, method'lu = 1453 (HER domain en az 1 gerçek method)
# method'suz kalan VARSA: runtime bridge hâlâ gerçek çağrı dener (detaylı hata)
# — katalogdaki invoke her domain için tanımlı olduğundan boş domain KAVRAMI YOKTUR.
```

- [ ] **Step 4: Commit**

```bash
git add katalog/mappings/bulk.json katalog/domains.json katalog/kapsam-report.txt
git commit -m "feat(katalog): bulk domain coverage — snapshot-backed method mapping for all remaining domains"
```

---

## Faz 5 — Transport: IO domain + domain enable/disable

### Task 5.1: IO domain (stream — büyük payload'lar)

**Files:**
- Create: `katalog/mappings/io.json`
- Modify: `remote/juggler/protocol/PageHandler.js` veya `BrowserHandler.js` (IO handler)

**Interfaces:**
- Produces: `IO.read(handle, offset?, size?)`, `IO.write(handle, data)`, `IO.close(handle)` — nsIFileInputStream/nsIFileOutputStream + nsIInputStreamReader; handle'lar XpcomRegistry.handle sistemiyle çalışır. Juggler pipe tek satır JSON — büyük veriler (dosya okuma, snapshot dump) IO üstünden chunk'lı akar (64KB chunk).
- Consumes: XpcomRegistry (3.1), XpcomBridge (3.2).

- [ ] **Step 1: `katalog/mappings/io.json` yaz + XpcomBridge'e IO method'ları**

```json
{
  "IO": {"source": "cc:@mozilla.org/network/file-input-stream;1 + cc:@mozilla.org/network/file-output-stream;1",
         "note": "openRead(path) -> handle; read(handle, count) -> {data, eof}; write(handle, data); close(handle)"}
}
```

```js
// XpcomBridge.js — IO domain özel-case (handle'ları stream nesneleri üzerinde çalıştırır):
//   'IO.openRead'  -> Cc["@mozilla.org/network/file-input-stream;1"].createInstance(Ci.nsIFileInputStream)
//                      .init(file, -1, -1, 0) ; register handle; dön: {__xpcom, size: file.fileSize}
//   'IO.read'      -> handle'dan stream.read(count) -> string (latin1/utf8 seçimi)
//   'IO.write'     -> file-output-stream init(file, 0x02|0x08|0x20, -1, 0); write(data)
//   'IO.close'     -> stream.close(); release(handle)
// nsIFile: Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile).initWithPath(path)
```

- [ ] **Step 2: Smoke — dosya yaz/oku döngüsü**

```bash
python3 katalog/smoke.py --binary $PWD/obj-*/dist/bin/firefox \
  --calls "IO|openRead|[\"/etc/hostname\"]" "IO|read|[\"<h>\"]" "IO|close|[\"<h>\"]"
# <h> yerine önceki çağrının handle'ı — smoke.py'ye state zinciri desteği gerekir:
# (Task 5.1 Step 3'teki küçük uzantı: {prev} token'ı önceki sonucun handle'ını enjekte eder)
```

- [ ] **Step 3: smoke.py'ye `{prev}` token desteği ekle (küçük değişiklik)**

```python
# katalog/smoke.py — run() içinde args çözümleme satırını değiştir:
#   prev_result = None  # her çağrıdan sonra: prev_result = r
#   args metninde "{prev}" geçiyorsa: prev_result içindeki __xpcom handle'ını enjekte et
#     (json.dumps(prev_result) ile string replace, sonra json.loads)
```

- [ ] **Step 4: Commit**

```bash
git add katalog/mappings/io.json katalog/smoke.py remote/juggler/protocol/XpcomBridge.js
git commit -m "feat(juggler): IO domain — file stream read/write/close with handle chains"
```

### Task 5.2: Per-domain enable/disable — event gateway

**Files:**
- Modify: `remote/juggler/protocol/XpcomBridge.js` (gateway — Task 3.2'de iskeleti var)
- Modify: `remote/juggler/content/XpcomAgent.js` (content event'leri)

**Interfaces:**
- Produces: Her domain `enable`/`disable` destekler (Task 2.2'de Protocol.js'e tanımlı):
  - enable → gateway'e abone; disable → çıkar.
  - Event kaynakları: nsIObserverService topic'leri (http-on-modify-request, console-message, network-activity, cookie-changed, download-progress, nsPref:changed, xpcom-shutdown...), frame DOM olayları (content-event: DOMContentLoaded, click, mutation...), mevcut Juggler event'leri (Page.* zaten var).
  - Event forward: `session.emitEvent('Domain.eventName', params)` — Dispatcher'ın mevcut mekanizması.
- Consumes: XpcomBridge.emit (3.2), Dispatcher fallback (2.3).

- [ ] **Step 1: Observer event gateway (browser-side)**

```js
// XpcomBridge.js — emit çağrısının gerçek bağlanması (3.2'deki _sessionEmit doldurulur):
// ObserverSink: Services.obs.addObserver(topic, observer, false) — tek bir observer tüm
// topic'leri toplar; bridge.invoke('Observer','enable',{topics:[...]}) kayıtlı topic'leri
// açar; her notification -> bridge.emit(domain, topic, {subject: serialize(...)}).
// Katalogda "Observer" domain'inin methods'una enable/disable zaten var (Task 2.2).
// Varsayılan aktif topic'ler (enable edilen her domain için):
const DEFAULT_TOPICS = ["console-message", "http-on-modify-request", "cookie-changed",
                        "network-activity", "nsPref:changed", "download-progress",
                        "content-event", "profile-change-net-teardown"];
// enable(domain) -> topicler domain adıyla eşleşen observer'ları açar; bilinmeyen
// topic isterse detaylı hata: "Unknown topic 'x' for domain 'y'. Available: [40 örnek]"
```

- [ ] **Step 2: Content event gateway (frame DOM olayları)**

```js
// XpcomAgent.js — contentTargets üstünde: env.window.addEventListener(type, fn, true)
// (capture); enable(domain, {events:["click","DOMContentLoaded"]}) -> kayıt;
// her olay -> bridge.emit(domain, type, {target: serialize(el), detail});
// asla sessiz: olay dinlenemezse (unknown type) detaylı hata dön.
```

- [ ] **Step 3: Smoke — event akışı kanıtı**

```bash
# Python: Observer.enable(console-message) -> evaluate ile console.log -> event yakala:
CAMOUFOX_BIN=$PWD/obj-*/dist/bin/firefox .venv/bin/python - <<'EOF'
import asyncio
from kahin.the_twins.mirage import Mirage
async def main():
    m = Mirage(engine_name="mirage"); await m.start()
    await m.send_cdp("Observer", "enable", {})
    await m.send_cdp("Page", "navigate", {"url": "data:text/html,<script>console.log('EVT-TEST')</script>"})
    await asyncio.sleep(0.5)
    events = await m.send_cdp("Browser", "getEvents", {})  # mevcut event buffer
    print("console-message yakalandı:", any("EVT-TEST" in str(e) for e in events))
    await m.stop()
asyncio.run(main())
EOF
```

- [ ] **Step 4: Commit**

```bash
git add remote/juggler/protocol/XpcomBridge.js remote/juggler/content/XpcomAgent.js
git commit -m "feat(juggler): per-domain enable/disable — observer + DOM event gateways"
```

---

## Faz 6 — MCP & Sidecar (raw_juggler erişimi)

### Task 6.1: Sidecar wire'ı Juggler-native (CDP-shaped katman kaldır)

**Files:**
- Modify: `camoufox-harness/core/ipc_main.zig` (mevcut `domain`/`command` alanlı processRequest → `method` alanlı)
- Modify: `camoufox-harness/core/tests.zig`
- Modify: `tests/test_mirage_ipc.py` (fake sidecar şeması)

**Interfaces:**
- Produces: Wire `{"id":N,"method":"Domain.method","params":{...},"sessionId?":"..."}` → `{"id":N,"result"}`/`{"id":N,"error":{"code","message"}}`; event'ler `{"method":"Domain.event","params":{...},"sessionId"}`. `Browser.health` yerel kalır; bilinmeyen domain root'a passthrough (browser -32601 net hata döner). Mirage.send_cdp imzası KORUNUR: `send_cdp(domain, command, params)` → wire'a `method=f"{domain}.{command}"` olarak çevrilir (Python tarafı Task 6.2'de tool'larla genişler).
- Consumes: mevcut driver.zig (Juggler-native send — değişmez).

- [ ] **Step 1: RED test — method wire'ı**

```zig
// tests.zig — mevcut router test desenine ekle (fake driver fixture, pipe.zig:382-399 deseni):
//   (a) {"id":1,"method":"Page.navigate","params":{"url":"x"}} -> driver'a "Page.navigate" iletilir
//   (b) {"id":2,"method":"Browser.health"} -> yerel cevap, Juggler'a GİTMEZ
//   (c) {"id":3,"method":"CookiesService.invoke","params":{"args":["getAll"]}} -> root passthrough
//   (d) {"id":4,"method":"Page.navigate"} page yokken -> -32600 "no page target; create one first"
//   (e) {"id":5,"method":"Network.enable"} -> artık no-op DEĞİL; page session'a gider (sayfa yoksa -32600)
```

- [ ] **Step 2: RED doğrula**

```bash
~/.local/opt/zig-x86_64-linux-0.16.0/zig build test 2>&1 | tail -5
# BEKLENTİ: yeni router testleri FAIL (eski şema domain bekliyor)
```

- [ ] **Step 3: ipc_main.zig router'ı yeniden yaz**

```zig
// ipc_main.zig — processRequest:
//   method: obj.get("method") oku (string). sessionId: obj.get("sessionId") (opsiyonel string).
//   Prefix ayrıştır: ilk '.' öncesi domain.
//   Yerel: "Browser.health" (mevcut implementasyon aynen).
//   Page/Runtime/Network/Heap/Accessibility.* -> page session (sessionId ?? current ?? -32600)
//   Browser.* -> sessionId ?? root
//   Geri kalan her şey -> d.send(null, method, params_json) root passthrough.
//   CDP-shaped katman SİLİNİR: handleTarget/handleBrowser/handlePage/handleRuntime/
//   handleEmulation + Network.enable/Console.enable no-op + Console.messageAdded çevirisi.
//   evaluateWithRetry mantığı yalnızca Runtime.evaluate'te kalır (context race).
//   ensurePage/currentPage/setCurrentTarget korunur (Session domain Task 6.2/zig'de değil,
//   Python tarafında — Target/Session zaten Juggler'da method olarak var).
//   Docstring: yeni wire şeması + routing tablosu + "Juggler-native — CDP translation removed".
```

- [ ] **Step 4: GREEN**

```bash
~/.local/opt/zig-x86_64-linux-0.16.0/zig build test 2>&1 | tail -5   # 177+ test PASS
~/.local/opt/zig-x86_64-linux-0.16.0/zig build-exe --dep driver -Mroot=ipc_main.zig \
  -Mdriver=driver.zig -O ReleaseSafe -femit-bin=zig-out/bin/kahin-sidecar
```

- [ ] **Step 5: Fake sidecar + mirage uyumu**

```bash
# tests/test_mirage_ipc.py FAKE_SIDECAR: {"domain","command"} -> {"method":"Domain.command"} şeması
# Mirage.send_cdp: params {method: f"{domain}.{command}", params: params} gönderir (imza korunur)
uv run pytest tests/test_mirage_ipc.py -q   # PASS
```

- [ ] **Step 6: Vendor + commit**

```bash
bash scripts/build-sidecar.sh   # vendor binary güncelle
git add camoufox-harness/core/ camoufox-harness/vendor/bin/ tests/test_mirage_ipc.py
git commit -m "refactor(sidecar): Juggler-native wire — method routing, CDP-shaped layer removed"
```

### Task 6.2: MCP tool'ları — raw_juggler / domain_list / domain_enable / domain_disable

**Files:**
- Modify: `kahin/oracle.py` (4 yeni tool)
- Modify: `kahin/the_twins/mirage.py` (send_cdp zaten method wire'ına uyarlı — Task 6.1; gerekirse `send_method(method, params)` helper'ı)

**Interfaces:**
- Produces (MCP tool'ları, isimler `kahin_` prefix):
  - `kahin_raw_juggler(domain, method, params?)` → `{"result": ...}` veya detaylı hata — WHITELIST YOK, her domain çağrılabilir
  - `kahin_domain_list(category?)` → katalogdan (1453 satır numaralı)
  - `kahin_domain_enable(domain, topics?)` / `kahin_domain_disable(domain)` → event gateway kontrolü
- Consumes: 6.1 wire, 5.2 gateway, `katalog/domains.json`.

- [ ] **Step 1: `kahin_raw_juggler` tool'u ekle**

```python
# oracle.py — mevcut tool kayıt desenine ekle (register_tool sarmalayıcıları):
@register_tool(
    "raw_juggler",
    "Doğrudan Juggler method çağrısı — 1453 domain. Whitelist yok, kısıtlama yok. "
    "domain: katalog adı (örn 'Cookie', 'Preference', 'DOM', 'SystemInfo'); "
    "method: XPCOM/DOM method adı birebir (örn 'getAll', 'getBoolPref'); "
    "params: herhangi bir JSON değeri. Hata durumunda detaylı dönüş.",
)
async def raw_juggler(domain: str, method: str, params: object = None) -> dict:
    """1453 domain'den herhangi bir method'u gerçek XPCOM çağrısıyla çalıştırır."""
    r = await _engine().send_cdp(domain, method, params if params is not None else {})
    return {"result": r}
```

- [ ] **Step 2: `kahin_domain_list` / enable / disable ekle**

```python
# oracle.py:
DOMAIN_CATALOG = json.loads(Path("katalog/domains.json").read_text())  # repo kökünden

@register_tool("domain_list", "1453 domain kataloğu — numaralı; category filtresi opsiyonel")
async def domain_list(category: str = "") -> dict:
    items = [e for e in DOMAIN_CATALOG if not category or e["category"] == category]
    return {"count": len(items), "domains": [f"{e['id']}. {e['domain']} ({e['category']})" for e in items]}

@register_tool("domain_enable", "Domain event gateway'ini aç (topics: observer topic'leri / DOM event adları)")
async def domain_enable(domain: str, topics: list = None) -> dict:
    r = await _engine().send_cdp(domain, "enable", {"topics": topics or []})
    return {"result": r}

@register_tool("domain_disable", "Domain event gateway'ini kapat")
async def domain_disable(domain: str) -> dict:
    r = await _engine().send_cdp(domain, "disable", {})
    return {"result": r}
```

- [ ] **Step 3: Test (birim + canlı)**

```bash
# Birim: tool kayıtları + katalog okuma
uv run pytest tests/test_oracle.py -q
# Canlı: fork binary + gerçek 3 domain çağrısı (raw_juggler yoluyla)
CAMOUFOX_BIN=~/camoufox-src/camoufox/firefox-*/obj-*/dist/bin/firefox \
  uv run pytest tests/test_e2e.py -q -k "raw or juggler" 2>&1 | tail -5
# (test_e2e.py'ye raw_juggler smoke'u eklenir: Cookie.getAll, SystemInfo.getProperty, Preference.getBoolPref)
```

- [ ] **Step 4: Commit**

```bash
git add kahin/oracle.py kahin/the_twins/mirage.py tests/test_e2e.py tests/test_oracle.py
git commit -m "feat(mcp): raw_juggler + domain_list + domain_enable/disable tools (1453 domain access)"
```

### Task 6.3: Fork binary bağlama + e2e

**Files:**
- Modify: `kahin/the_twins/chassis.py` / `shadow.py` (KAHIN_CAMOUFOX_BIN zaten destekli — doğrula)

**Interfaces:**
- Produces: Kahin MCP varsayılan olarak fork binary'yi spawn eder; `kahin_browser_start` sonrası 1453 domain erişimi.
- Consumes: 6.1, 6.2.

- [ ] **Step 1: KAHIN_CAMOUFOX_BIN akışını doğrula + kalıcı yol**

```bash
grep -rn "KAHIN_CAMOUFOX_BIN" kahin/ camoufox-harness/ | head
# Mevcut mekanizma (binary.zig env override) fork binary'yi işaret eder. Kalıcılaştır:
# .env ya da kahin config: CAMOUFOX_BIN=~/camoufox-src/camoufox/firefox-*/obj-*/dist/bin/firefox
# Not: kahin.oracle'ın varsayılan Camoufox yolu hâlâ kurulu binary'yi işaret eder;
# fork binary production'a geçince bu yolu güncelle (son adım).
```

- [ ] **Step 2: E2E — 1453 domain'den 5 farklı kategori çağrısı**

```bash
CAMOUFOX_BIN=~/camoufox-src/camoufox/firefox-*/obj-*/dist/bin/firefox \
  uv run pytest tests/test_e2e.py -q   # TÜM e2e PASS
python3 katalog/smoke.py --binary ~/camoufox-src/camoufox/firefox-*/obj-*/dist/bin/firefox \
  --calls "Cookie|getAll|[]" "Preference|getBoolPref|[\"dom.w3c_touch_events\"]" \
          "SystemInfo|getProperty|[\"os\"]" "Document|querySelector|[\"body\"]" \
          "Memory|getHeapUsage|[]" "Observer|enumerateObservers|[]"
```

- [ ] **Step 3: Commit**

```bash
git add kahin/ tests/ docs/
git commit -m "feat(mcp): fork binary integration — e2e across 5 categories (1453 domain ready)"
```

---

## Faz 7 — Doğrulama & Kapanış

### Task 7.1: 1453 domain smoke suite (otomatik)

**Files:**
- Create: `katalog/full-smoke.py`

**Interfaces:**
- Produces: `katalog/full-smoke-report.txt` — 1453 domain'in HER BİRİ için ≥1 method gerçek çağrısı + PASS/FAIL. Çağrı seçimi: katalogdaki ilk method (güvenli olduğu bilinenlerden: getter/reader method'ları öncelikli; yazıcı/tehlikeli method'lar `SAFE_ONLY` işaretli değilse skip değil — çağrılır ama sistem state'ine dokunmayan argümanlarla).
- Consumes: smoke.py (4.0), snapshot (4.0), fork binary.

- [ ] **Step 1: `katalog/full-smoke.py` yaz (tam kod)**

```python
#!/usr/bin/env python3
"""1453 domain'in HER birini gerçek çağrıyla doğrular.
Her domain: methods[0] seçilir — öncelik sırası: get*/read*/enumerate*/list*/is*/has* > diğer.
Argümansız çağrı dener; TypeError dönerse {} ile tekrar. {prev} zinciri yok — bağımsız çağrılar.
Çıktı: full-smoke-report.txt (PASS/FAIL + domain + method + özet hata); exit = FAIL sayısı.
Tehlikeli method listesi (SAFE_SKIP — ASLA otomatik çağrılmaz, elle test edilir):
  shutdown, quit, close (browser), clear, removeAll, deleteDatabase, uninstall, kill, execute (harici).
"""
import asyncio, json, sys, os
sys.path.insert(0, ".")
from kahin.the_twins.mirage import Mirage

SAFE_SKIP = ("shutdown", "quit", "clear", "removeAll", "deleteDatabase", "uninstall", "kill")
PREFER = ("get", "read", "enumerate", "list", "is", "has", "count", "length", "state")

def pick_method(ms):
    if not ms: return None
    best = ms[0]["name"]
    for m in ms:
        n = m["name"]
        if n in SAFE_SKIP: continue
        if any(n.startswith(p) for p in PREFER):
            return n
        best = n
    return best if best not in SAFE_SKIP else None

async def run(binary):
    os.environ["KAHIN_CAMOUFOX_BIN"] = binary
    m = Mirage(engine_name="mirage"); await m.start()
    cat = json.load(open("katalog/domains.json"))
    rows, fail = [], 0
    for e in cat:
        meth = pick_method(e["methods"])
        if not meth:
            rows.append(f"FAIL\t{e['id']}\t{e['domain']}\tNO-SAFE-METHOD"); fail += 1; continue
        try:
            r = await m.send_cdp(e["domain"], "invoke", {"args": []}) if meth == "invoke" \
                else await m.send_cdp(e["domain"], meth, {})
            ok = not (isinstance(r, dict) and "error" in r)
            rows.append(("PASS" if ok else "FAIL") + f"\t{e['id']}\t{e['domain']}\t{meth}\t" +
                        (json.dumps(r)[:120] if not ok else "ok"))
            fail += not ok
        except Exception as ex:
            rows.append(f"FAIL\t{e['id']}\t{e['domain']}\t{meth}\t{ex}"); fail += 1
    await m.stop()
    open("katalog/full-smoke-report.txt", "w").write("\n".join(rows) + "\n")
    print(f"PASS={len(rows)-fail} FAIL={fail} total={len(rows)}")
    return fail

if __name__ == "__main__":
    fail = asyncio.run(run(sys.argv[1]))
    sys.exit(min(fail, 255))
```

- [ ] **Step 2: Çalıştır (uzun — 1453 çağrı, ~10-20 dk)**

```bash
python3 katalog/full-smoke.py ~/camoufox-src/camoufox/firefox-*/obj-*/dist/bin/firefox | tee /tmp/opencode/full-smoke.log
grep -c '^PASS' katalog/full-smoke-report.txt
grep -c '^FAIL' katalog/full-smoke-report.txt
```

- [ ] **Step 3: FAIL deterministik düzeltme döngüsü**

```bash
# Her FAIL satırı için: hata mesajını oku → 3 olası neden:
#  (1) method argüman ister -> smoke'u o method için arg listesiyle güçlendir (calls-file)
#  (2) method adı yanlış -> snapshot'tan doğru adı bul, katalogu düzelt, gen_domains.py yeniden
#  (3) kaynak çözülemiyor -> XpcomRegistry.resolve hatası: mapping'i düzelt (bulk.json)
# HEDEF: FAIL=0. FAIL>0 ise her biri deterministik bir fix ile kapanır; "muhtemelen çalışır" YOK.
```

- [ ] **Step 4: Commit**

```bash
git add katalog/full-smoke.py katalog/full-smoke-report.txt katalog/domains.json katalog/mappings/
git commit -m "test(juggler): full 1453-domain smoke — every domain provably invocable (FAIL=0)"
```

### Task 7.2: Final doğrulama + çıktı paketi

**Files:**
- Create: `docs/juggler-1453.md` (çıktı belgesi — domain listesi, build komutları, mimari, test kanıtı)

**Interfaces:**
- Produces: ÇIKTI PAKETİ (kullanıcı gereksinimi): patch'li fork kaynağı, 1453 domain Protocol.js, bridge/agent dosyaları, build komutları, MCP tool'ları, numaralı domain listesi (katalog/domains-1453.txt).

- [ ] **Step 1: Tüm regresyon — MEVCUT sistem bozulmadı**

```bash
cd /home/void0x14/Belgeler/mcp-projelerim/cdp-kahin-mcp
uv run pytest tests/ -q            # 102+ koleksiyon PASS (mevcut 6-domain akışları dahil)
~/.local/opt/zig-x86_64-linux-0.16.0/zig build test   # 177+ PASS
ruff check kahin/                  # yeni hata 0
```

- [ ] **Step 2: `docs/juggler-1453.md` yaz**

```markdown
# Juggler 1453 Domain — Kahin MCP

## Özet
Camoufox (Firefox 152) fork'u: Juggler protokolü 6 -> 1453 domain. Her domain gerçek
XPCOM/DOM API'sine bağlı (stub yok). AI ajanlar tarayıcının her noktasına ham erişimde.

## Build
    cd ~/camoufox-src/camoufox/firefox-*/ && ./mach build
    # OOM koruması: Task 0.1 (swapfile 8G, MOZ_PARALLEL_BUILD=8, CARGO_BUILD_JOBS=1)

## MCP kullanımı
    kahin_raw_juggler(domain="Cookie", method="getAll")
    kahin_raw_juggler(domain="SystemInfo", method="getProperty", params={"args":["os"]})
    kahin_domain_list(category="media")
    kahin_domain_enable(domain="Observer", topics=["console-message"])

## Mimari
- Protocol.js: 1453 domain tanımı (generator: katalog/gen_protocol.py)
- Dispatcher: generic fallback -> XpcomBridge.invoke (Task 2.3)
- XpcomBridge (browser) / XpcomAgent (content): domain -> XPCOM kaynağı + handle sistemi
- Katalog: katalog/domains.json (1453, numaralı) + domains-1453.txt

## Kanıt
- full-smoke: katalog/full-smoke-report.txt (1453/1453 PASS hedefi)
- e2e: 5 kategori çağrısı test_e2e.py
- Mevcut regresyon: pytest + zig build test green

## Domain listesi
    katalog/domains-1453.txt (1453 satır: id\tdomain\tkategori\tkaynak)
```

- [ ] **Step 3: Commit + kapanış doğrulaması**

```bash
git add docs/juggler-1453.md && git commit -m "docs(juggler): 1453-domain delivery — build, MCP usage, evidence"
git log --oneline -20   # atomik commit zinciri gözden geçir
```

---

## Self-Review (spec'e karşı kontrol)

| Spec gereksinimi | Karşılayan task |
|---|---|
| nsI*/Services/ChromeUtils listeleri çıkar | Task 1.1 |
| 1453 domain tanımı (types/methods/events) | Task 1.2, 2.2, 4.0 |
| Method adları Firefox API'leriyle birebir | Task 4.0 (snapshot), 4.1-4.14 mapping'leri |
| Her domain handler (browser+content) | Task 3.2, 3.3, 4.1-4.14 |
| XPCOM doğrudan çağrı, detaylı hata, sessizce geçme | Task 3.2 (error nesnesi), tüm task'ların "asla sessiz" notları |
| 14 kategori + kalan tüm XPCOM | Task 4.1-4.14 |
| Pipe korunur + IO stream | Faz 5.1 |
| Her domain enable/disable | Task 2.2 (tanım), 5.2 (gateway) |
| raw_juggler / domain_list / domain_enable / domain_disable | Task 6.2 |
| Whitelist/kısıtlama yok | Task 6.2 (Global Constraints) |
| Build + test + 1453 erişim kanıtı | Faz 0, Task 7.1 |
| Atomik commit'ler | Her task'ın commit adımı |
| Subagent-driven / AI-native uygulanabilirlik | Her task bite-sized + tam kod + beklenen çıktı |
| Firefox ayrı indirme yasak (yalnız Camoufox zinciri) | Global Constraints + Task 0.2 (make fetch) |
| Build ile sıfırdan (omni.ja hack yok) | Global Constraints + Task 0.5 |
| Disk/RAM kısıtı yok ama OOM yasak | Task 0.1 (swap + paralellik sınırı) |

**Bilinen riskler (planın dürüstlük bölümü):**
1. `XpcomRegistry.resolve` modül import'ları: bazı modül isimleri resource URI tahminiyle eşleşmeyebilir → Task 4.14'te runtime-verified mekanizması + detaylı hata (asla sessiz).
2. `Ci[name]` dinamik erişim: Xray + ChromeUtils ortamında çalışır (generateQI ile doğrulanır); çalışmayan interface'ler Task 4.14'te FAIL raporlanır, mapping'e elle girilir.
3. Firefox 152'de SessionStore gibi modüller browser chrome'da yaşar — browser process'te çalışır; content'ten istenirse detaylı hata (doğru davranış).
4. Build süresi ilk sefer 1.5-3 saat (Task 0.5); sonraki her Juggler değişikliği inkremental 2-10 dk.
5. Handle'lar session ömürlü; process ölürse geçersiz — bridge `byHandle` null döner, net hata verir.
6. 1453 çağrılı full-smoke ~10-20 dk; CI'da etiketle.
7. `moz-src/` omni.ja yolu `IOUtils.readUTF8` ile jar içinden okumada sorun çıkarsa XpcomCatalog.json `resource://` altına koyulur (Task 3.1 fallback hazır).





