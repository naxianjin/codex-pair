"use strict";
const path = require("path");
const { spawnSync } = require("child_process");
const { TextDecoder } = require("util");

module.exports = function readWindowsInput(file, maxBytes) {
  const systemRoot = process.env.SystemRoot || process.env.SYSTEMROOT;
  if (!systemRoot || !path.win32.isAbsolute(systemRoot)) throw new Error("Windows system directory unavailable");
  const shell = path.join(systemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
  const result = spawnSync(shell, ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
    "-File", path.join(__dirname, "read-windows-input.ps1"), "-LiteralPath", path.resolve(file)],
    { shell: false, windowsHide: true, timeout: 15000, maxBuffer: maxBytes + 1 });
  if (result.error || result.status !== 0 || result.stdout.length > maxBytes) {
    throw new Error("Windows handle-based input read failed or exceeded its limit");
  }
  try { return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(result.stdout); }
  catch { throw new Error("input is not valid UTF-8"); }
};
