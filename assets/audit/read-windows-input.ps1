param([Parameter(Mandatory=$true)][string]$LiteralPath)
$ErrorActionPreference='Stop'
$source=$null
try {
 $source=[IO.File]::Open((Join-Path $PSScriptRoot 'WindowsSafeInput.cs'),'Open','Read','Read')
 $reader=New-Object IO.StreamReader($source,[Text.Encoding]::UTF8,$true,4096,$true)
 try {$code=$reader.ReadToEnd()} finally {$reader.Dispose()}
 Add-Type -TypeDefinition $code
 $bytes=[PairAuditInput]::Read($LiteralPath)
 $stdout=[Console]::OpenStandardOutput()
 $stdout.Write($bytes,0,$bytes.Length)
 $stdout.Flush()
} catch {
 [Console]::Error.WriteLine('Windows input could not be read safely')
 exit 1
} finally {
 if($source){$source.Dispose()}
}
