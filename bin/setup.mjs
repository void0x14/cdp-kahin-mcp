#!/usr/bin/env node
// kahin auto-setup — kurulu AI CLI istemcilerini tespit eder ve kahin MCP'yi kaydeder.
// Idempotent: zaten kayıtlıysa atlar, mevcut config'leri merge eder (ezmez).
// Opt-out: KAHIN_SKIP_AUTO_SETUP=1
// El ile: kahin setup

import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join, dirname } from "node:path";

const HOME = homedir();
const isWin = process.platform === "win32";
const ENTRY = { command: "kahin", args: [] };
const ENTRY_OPENCODE = { type: "local", command: ["kahin"], enabled: true };
const ENTRY_VSCODE = { type: "stdio", command: "kahin", args: [] };

const CLIENTS = [
  {
    name: "Claude Code",
    file: join(HOME, isWin ? ".claude.json" : ".claude.json"),
    root: "mcpServers",
    entry: ENTRY,
  },
  {
    name: "Claude Desktop",
    file: isWin
      ? join(process.env.APPDATA || "", "Claude", "claude_desktop_config.json")
      : join(HOME, ".config", "Claude", "claude_desktop_config.json"),
    root: "mcpServers",
    entry: ENTRY,
  },
  {
    name: "Cursor",
    file: join(HOME, ".cursor", "mcp.json"),
    root: "mcpServers",
    entry: ENTRY,
  },
  {
    name: "Windsurf",
    file: join(HOME, ".codeium", "windsurf", "mcp_config.json"),
    root: "mcpServers",
    entry: ENTRY,
  },
  {
    name: "opencode",
    file: join(HOME, ".config", "opencode", "opencode.json"),
    root: "mcp",
    entry: ENTRY_OPENCODE,
  },
  {
    name: "Gemini CLI",
    file: join(HOME, ".gemini", "settings.json"),
    root: "mcpServers",
    entry: ENTRY,
  },
  {
    name: "Zed",
    file: join(HOME, ".config", "zed", "settings.json"),
    root: "context_servers",
    entry: ENTRY,
  },
  {
    name: "VS Code",
    file: isWin
      ? join(process.env.APPDATA || "", "Code", "User", "mcp.json")
      : join(HOME, ".config", "Code", "User", "mcp.json"),
    root: "servers",
    entry: ENTRY_VSCODE,
  },
  {
    name: "Codex CLI",
    file: join(HOME, ".codex", "config.toml"),
    root: "mcp_servers",
    entry: ENTRY,
    format: "toml",
  },
];

function log(msg) {
  process.stderr.write(`[kahin] ${msg}\n`);
}

function upsertJson(client) {
  const { file, root, entry, name } = client;
  let cfg = {};
  try {
    cfg = JSON.parse(readFileSync(file, "utf8"));
  } catch {
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
    if (client.format === "toml") {
      if (upsertToml(client)) installed++;
    } else if (upsertJson(client)) {
      installed++;
    }
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
