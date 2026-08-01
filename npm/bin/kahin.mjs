#!/usr/bin/env node
// kahin global launcher — ilk çalıştırmada Python venv kurar, PyPI'dan kahin indirir.
// Kurulum: pnpm add -g kahin

import { spawn } from "node:child_process";
import { mkdirSync, existsSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

const KAHIN_VERSION = "0.1.0";
const HOME = process.env.KAHIN_HOME || join(homedir(), ".local", "share", "kahin");
const VENV = join(HOME, "venv");
const PY = process.platform === "win32" ? join(VENV, "Scripts", "python.exe") : join(VENV, "bin", "python");
const MARKER = join(HOME, "version");
const LOG = join(tmpdir(), "kahin-setup.log");

function sh(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    const c = spawn(cmd, args, { stdio: ["ignore", "ignore", "pipe"], ...opts });
    c.stderr.on("data", (d) => process.stderr.write(`[kahin] ${d}`));
    c.on("close", (code) => resolve(code));
  });
}

async function setup() {
  mkdirSync(HOME, { recursive: true });
  const needsInstall = !existsSync(PY) || !existsSync(MARKER) || (await import("node:fs/promises")).readFile(MARKER, "utf8").catch(() => "") !== KAHIN_VERSION;
  if (!needsInstall) return;

  process.stderr.write("[kahin] Python ortamı kuruluyor (ilk sefer) — ~30 sn...\n");
  const start = Date.now();
  const index = process.env.KAHIN_FIND_LINKS ? ["--find-links", process.env.KAHIN_FIND_LINKS] : [];
  let code;
  if (process.env.UV && existsSync(process.env.UV)) {
    code = await sh(process.env.UV, ["venv", "--python", "3.12", VENV]);
    if (code === 0) code = await sh(process.env.UV, ["pip", "install", "--python", PY, `kahin==${KAHIN_VERSION}`, ...index]);
  } else if (existsSync(join(homedir(), ".local", "bin", "uv"))) {
    code = await sh(join(homedir(), ".local", "bin", "uv"), ["venv", "--python", "3.12", VENV]);
    if (code === 0) code = await sh(join(homedir(), ".local", "bin", "uv"), ["pip", "install", "--python", PY, `kahin==${KAHIN_VERSION}`, ...index]);
  } else {
    code = await sh("python3", ["-m", "venv", VENV]);
    if (code === 0) code = await sh(PY, ["-m", "pip", "install", "-U", "pip", "-q"], { env: { ...process.env, PIP_DISABLE_PIP_VERSION_CHECK: "1" } });
    if (code === 0) code = await sh(PY, ["-m", "pip", "install", `kahin==${KAHIN_VERSION}`, ...index]);
  }
  if (code !== 0) {
    process.stderr.write(`[kahin] Kurulum başarısız. Log: ${LOG} — elle kur: python3 -m venv ${VENV} && ${PY} -m pip install kahin==${KAHIN_VERSION}\n`);
    process.exit(1);
  }
  (await import("node:fs/promises")).writeFile(MARKER, KAHIN_VERSION);
  process.stderr.write(`[kahin] Hazır (${((Date.now() - start) / 1000).toFixed(0)} sn).\n`);
}

const argv = process.argv.slice(2);
const upgrade = argv.includes("--upgrade") || argv.includes("--update");
const args = argv.filter((a) => a !== "--upgrade" && a !== "--update");
if (upgrade) {
  await import("node:fs/promises").then((fs) => fs.rm(MARKER, { force: true }));
}

await setup();
const child = spawn(PY, ["-m", "kahin.oracle", ...args], { stdio: "inherit" });
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code ?? 0);
});
