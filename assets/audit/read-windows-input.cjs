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
    { shell: false, windowsHide: true, timeout: 15000, maxBuffer: Math.max(maxBytes + 1, 65536) });
  if (result.error || result.status !== 0 || result.stdout.length > maxBytes || result.stderr.length > 65536) {
    throw new Error("Windows handle-based input read failed or exceeded its limit");
  }
  let observation;
  const line = result.stderr.toString("utf8").trimEnd();
  if (!line.startsWith("PAIR_READER_OBSERVATION:") || /[\r\n]/u.test(line)) {
    throw new Error("Windows reader observation unavailable");
  }
  try { observation = JSON.parse(line.slice("PAIR_READER_OBSERVATION:".length)); }
  catch { throw new Error("Windows reader observation invalid"); }
  if (observation.schema_version !== 1 || observation.formal !== false ||
      !Number.isSafeInteger(observation.pid) || observation.pid <= 0 ||
      !Array.isArray(observation.methods) || observation.methods.length !== 3) {
    throw new Error("Windows reader observation invalid");
  }
  let text;
  try { text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(result.stdout); }
  catch { throw new Error("input is not valid UTF-8"); }
  process.stderr.write(line + "\n");
  return text;
};
