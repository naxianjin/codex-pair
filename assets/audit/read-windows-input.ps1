param([Parameter(Mandatory=$true)][string]$LiteralPath)
$ErrorActionPreference='Stop'
try {
 Add-Type -Path (Join-Path $PSScriptRoot 'WindowsSafeInput.cs')
 $bytes=[PairAuditInput]::Read($LiteralPath)
 $stdout=[Console]::OpenStandardOutput()
 $stdout.Write($bytes,0,$bytes.Length)
 $stdout.Flush()
} catch {
 [Console]::Error.WriteLine('Windows input could not be read safely')
 exit 1
}
