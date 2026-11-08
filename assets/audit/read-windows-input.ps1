param([Parameter(Mandatory=$true)][string]$LiteralPath)
$ErrorActionPreference='Stop'
$source=$null
$dll=$null
$workspace=$null
$sha=[Security.Cryptography.SHA256]::Create()
function Hash-Bytes([byte[]]$data){[BitConverter]::ToString($sha.ComputeHash($data)).Replace('-','').ToLowerInvariant()}
try {
 $source=[IO.File]::Open((Join-Path $PSScriptRoot 'WindowsSafeInput.cs'),'Open','Read','Read')
 $memory=New-Object IO.MemoryStream
 try {$source.CopyTo($memory);$sourceBytes=$memory.ToArray()} finally {$memory.Dispose()}
 $text=New-Object IO.MemoryStream(,$sourceBytes)
 $reader=New-Object IO.StreamReader($text,[Text.Encoding]::UTF8,$true)
 try {$code=$reader.ReadToEnd()} finally {$reader.Dispose()}
 $workspace=Join-Path ([IO.Path]::GetTempPath()) ('pair-reader-'+[guid]::NewGuid().ToString('N'))
 [IO.Directory]::CreateDirectory($workspace) | Out-Null
 $assemblyPath=Join-Path $workspace 'reader.dll'
 Add-Type -TypeDefinition $code -OutputAssembly $assemblyPath
 $dll=[IO.File]::Open($assemblyPath,'Open','Read','Read')
 $memory=New-Object IO.MemoryStream
 try {$dll.CopyTo($memory);$assemblyBytes=$memory.ToArray()} finally {$memory.Dispose()}
 $assembly=[Reflection.Assembly]::Load($assemblyBytes)
 $type=$assembly.GetType('PairAuditInput',$true)
 $methods=@($type.GetMethods([Reflection.BindingFlags]'Static,Public,NonPublic,DeclaredOnly') | ForEach-Object {
  $body=$_.GetMethodBody()
  if($body){$il=$body.GetILAsByteArray();@{metadata_token=$_.MetadataToken;signature=$_.ToString();il_sha256=(Hash-Bytes $il);il_bytes=$il.Length}}
 })
 if($methods.Count -ne 3){throw 'Managed reader method set mismatch'}
 $arguments=New-Object object[] 1
 $arguments[0]=$LiteralPath
 $bytes=$type.GetMethod('Read').Invoke($null,$arguments)
 $observation=@{schema_version=1;formal=$false;pid=$PID;process_creation_utc=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o');source_sha256=(Hash-Bytes $sourceBytes);assembly_input_sha256=(Hash-Bytes $assemblyBytes);assembly_input_base64=[Convert]::ToBase64String($assemblyBytes);mvid=$type.Module.ModuleVersionId.ToString();methods=$methods}
 [Console]::Error.WriteLine('PAIR_READER_OBSERVATION:'+($observation | ConvertTo-Json -Depth 4 -Compress))
 $stdout=[Console]::OpenStandardOutput()
 $stdout.Write($bytes,0,$bytes.Length)
 $stdout.Flush()
} catch {
 [Console]::Error.WriteLine('Windows input could not be read safely')
 exit 1
} finally {
 if($dll){$dll.Dispose()}
 if($source){$source.Dispose()}
 $sha.Dispose()
 if($workspace){
  if([IO.File]::Exists($assemblyPath)){[IO.File]::Delete($assemblyPath)}
  [IO.Directory]::Delete($workspace,$false)
 }
}
