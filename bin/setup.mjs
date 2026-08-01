#!/usr/bin/env node
// kahin auto-setup — kurulu AI CLI istemcilerini tespit eder ve kahin MCP'yi kaydeder.
// Idempotent: zaten kayıtlıysa atlar, mevcut config'leri merge eder (ezmez).
// Opt-out: KAHIN_SKIP_AUTO_SETUP=1
// El ile: kahin setup
//
// Client listesi, otomatik tespit yapan sistemlerden derlendi (add-mcp 15 ajan,
// everymcp 15, mcpm 20+, getmcp 19, mcp-get 9, mcpkit): 22 client.

import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join, dirname } from "node:path";

const HOME = homedir();
const isWin = process.platform === "win32";
const isMac = process.platform === "darwin";
const APPDATA = process.env.APPDATA || join(HOME, "AppData", "Roaming");

const ENTRY = { command: "kahin", args: [] };
const ENTRY_OPENCODE = { type: "local", command: ["kahin"], enabled: true };
const ENTRY_VSCODE = { type: "stdio", command: "kahin", args: [] };

// VS Code uzantı globalStorage yolu (Cline/Roo/Kilo burada yaşar)
function vscodeStorage(...ext) {
  const base = isWin
    ? join(APPDATA, "Code", "User", "globalStorage")
    : isMac
      ? join(HOME, "Library", "Application Support", "Code", "User", "globalStorage")
      : join(HOME, ".config", "Code", "User", "globalStorage");
  return join(base, ...ext);
}

// her client: { name, file, root, entry, format }
const CLIENTS = [
  // ---- JSON: root "mcpServers" ----
  {
    name: "Claude Code",
    file: join(HOME, ".claude.json"),
    root: "mcpServers",
  },
  {
    name: "Claude Desktop",
    file: isWin
      ? join(APPDATA, "Claude", "claude_desktop_config.json")
      : join(HOME, ".config", "Claude", "claude_desktop_config.json"),
    root: "mcpServers",
  },
  {
    name: "Cursor",
    file: join(HOME, ".cursor", "mcp.json"),
    root: "mcpServers",
  },
  {
    name: "Windsurf",
    file: join(HOME, ".codeium", "windsurf", "mcp_config.json"),
    root: "mcpServers",
  },
  {
    name: "Gemini CLI",
    file: join(HOME, ".gemini", "settings.json"),
    root: "mcpServers",
  },
  {
    name: "Cline (VS Code)",
    file: vscodeStorage("saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
    root: "mcpServers",
  },
  {
    name: "Cline CLI",
    file: join(HOME, ".cline", "data", "settings", "cline_mcp_settings.json"),
    root: "mcpServers",
  },
  {
    name: "Roo Code",
    file: vscodeStorage("rooveterinaryinc.roo-cline", "settings", "mcp_settings.json"),
    root: "mcpServers",
  },
  {
    name: "Kilo Code",
    file: vscodeStorage("kilocode.kilo-code", "settings", "mcp_settings.json"),
    root: "mcpServers",
  },
  {
    name: "Continue",
    file: join(HOME, ".continue", "config.json"),
    root: "mcpServers",
  },
  {
    name: "Amazon Q",
    file: join(HOME, ".aws", "amazonq", "mcp.json"),
    root: "mcpServers",
  },
  {
    name: "Trae",
    file: join(HOME, ".trae", "mcp.json"),
    root: "mcpServers",
  },
  {
    name: "BoltAI",
    file: isMac
      ? join(HOME, "Library", "Application Support", "boltAI", "config.json")
      : join(HOME, ".boltai", "config.json"),
    root: "mcpServers",
  },
  {
    name: "Antigravity",
    file: join(HOME, ".gemini", "config", "mcp_config.json"),
    root: "mcpServers",
  },
  {
    name: "Amp",
    file: join(HOME, ".amp", "config.json"),
    root: "mcpServers",
  },
  {
    name: "MCPorter",
    file: join(HOME, ".mcporter", "mcporter.json"),
    root: "mcpServers",
  },
  {
    name: "GitHub Copilot CLI",
    file: join(HOME, ".copilot", "mcp-config.json"),
    root: "mcpServers",
  },
  // ---- JSON: özel root ----
  {
    name: "Zed",
    file: isWin
      ? join(APPDATA, "Zed", "settings.json")
      : isMac
        ? join(HOME, "Library", "Application Support", "Zed", "settings.json")
        : join(HOME, ".config", "zed", "settings.json"),
    root: "context_servers",
  },
  {
    name: "opencode",
    file: join(HOME, ".config", "opencode", "opencode.json"),
    root: "mcp",
    entry: ENTRY_OPENCODE,
  },
  {
    name: "VS Code",
    file: isWin
      ? join(APPDATA, "Code", "User", "mcp.json")
      : isMac
        ? join(HOME, "Library", "Application Support", "Code", "User", "mcp.json")
        : join(HOME, ".config", "Code", "User", "mcp.json"),
    root: "servers",
    entry: ENTRY_VSCODE,
  },
  // ---- TOML ----
  {
    name: "Codex CLI",
    file: join(HOME, ".codex", "config.toml"),
    root: "mcp_servers",
    format: "toml",
  },
  // ---- YAML ----
  {
    name: "Goose",
    file: join(HOME, ".config", "goose", "config.yaml"),
    root: "extensions",
    format: "yaml",
  },
];

function log(msg) {
  process.stderr.write(`[kahin] ${msg}\n`);
}

// JSONC (yorumlu JSON) için basit strip — // ve /* */ satırları temizler.
function stripJsonc(src) {
  let out = "";
  let i = 0;
  const n = src.length;
  let inStr = false;
  while (i < n) {
    const c = src[i];
    const nx = src[i + 1];
    if (inStr) {
      out += c;
      if (c === "\\") {
        out += nx ?? "";
        i += 2;
        continue;
      }
      if (c === '"') inStr = false;
      i++;
      continue;
    }
    if (c === '"') {
      inStr = true;
      out += c;
      i++;
      continue;
    }
    if (c === "/" && nx === "/") {
      while (i < n && src[i] !== "\n") i++;
      continue;
    }
    if (c === "/" && nx === "*") {
      i += 2;
      while (i < n && !(src[i] === "*" && src[i + 1] === "/")) i++;
      i += 2;
      continue;
    }
    out += c;
    i++;
  }
  return out;
}

function readJson(file) {
  try {
    return JSON.parse(stripJsonc(readFileSync(file, "utf8")));
  } catch {
    return undefined;
  }
}

function upsertJson(client) {
  const { file, root, name } = client;
  const entry = client.entry || ENTRY;
  const cfg = readJson(file);
  if (cfg === undefined) {
    log(`${name}: config okunamadı (bozuk JSON?), atlandı`);
    return false;
  }
  if (typeof cfg !== "object" || cfg === null || Array.isArray(cfg)) {
    log(`${name}: beklenmeyen config yapısı, atlandı`);
    return false;
  }
  const rootObj = cfg[root];
  if (rootObj !== undefined && typeof rootObj !== "object") {
    log(`${name}: beklenmeyen "${root}" yapısı, atlandı`);
    return false;
  }
  cfg[root] = cfg[root] || {};
  if (cfg[root]["kahin"]) {
    log(`${name}: zaten kayıtlı, atlandı`);
    return false;
  }
  cfg[root]["kahin"] = entry;
  writeFileSync(file, JSON.stringify(cfg, null, 2) + "\n");
  log(`${name}: kaydedildi (${file})`);
  return true;
}

function upsertToml(client) {
  const { file, name } = client;
  let content = "";
  try {
    content = readFileSync(file, "utf8");
  } catch {
    log(`${name}: config okunamadı, atlandı`);
    return false;
  }
  if (content.includes("[mcp_servers.kahin]")) {
    log(`${name}: zaten kayıtlı, atlandı`);
    return false;
  }
  content += `\n[mcp_servers.kahin]\ncommand = "kahin"\nargs = []\n`;
  writeFileSync(file, content);
  log(`${name}: kaydedildi (${file})`);
  return true;
}

function upsertYaml(client) {
  const { file, name } = client;
  let content = "";
  try {
    content = readFileSync(file, "utf8");
  } catch {
    log(`${name}: config okunamadı, atlandı`);
    return false;
  }
  // extensions altında kahin bloğu var mı?
  if (/^extensions:\s*$[\s\S]*?^  kahin:/m.test(content)) {
    log(`${name}: zaten kayıtlı, atlandı`);
    return false;
  }
  if (!/^extensions:/m.test(content)) {
    content += "\nextensions:\n";
  }
  content += "  kahin:\n    cmd: kahin\n    enabled: true\n";
  writeFileSync(file, content);
  log(`${name}: kaydedildi (${file})`);
  return true;
}

export function setup() {
  if (process.env.KAHIN_SKIP_AUTO_SETUP || process.env.CI) {
    log("otomatik kurulum atlandı (KAHIN_SKIP_AUTO_SETUP/CI)");
    return { skipped: true };
  }
  let installed = 0;
  let found = 0;
  for (const client of CLIENTS) {
    if (!existsSync(client.file)) continue;
    found++;
    mkdirSync(dirname(client.file), { recursive: true });
    const ok =
      client.format === "toml"
        ? upsertToml(client)
        : client.format === "yaml"
          ? upsertYaml(client)
          : upsertJson(client);
    if (ok) installed++;
  }
  if (installed === 0) {
    if (found > 0) {
      log("tüm tespit edilen istemcilerde kahin zaten kayıtlı.");
    } else {
      log("desteklenen AI CLI aracı bulunamadı. `kahin setup` ile sonradan çalıştır.");
    }
  } else {
    log(`tamamlandı: ${installed} istemciye kaydedildi. Yeni kurulum sonrası aracı yeniden başlat.`);
  }
  return { installed };
}

if (process.argv[1] && process.argv[1].endsWith("setup.mjs")) {
  setup();
}
