param(
 [Parameter(Mandatory=$true)][string]$AssemblyPath,
 [Parameter(Mandatory=$true)][string]$ExpectedSHA256,
 [Parameter(Mandatory=$true)][string]$LiteralPath
)
$ErrorActionPreference='Stop'
$dll=$null
$sha=[Security.Cryptography.SHA256]::Create()
function Hash-Bytes([byte[]]$data){[BitConverter]::ToString($sha.ComputeHash($data)).Replace('-','').ToLowerInvariant()}
try {
 if($AssemblyPath -notmatch '^[A-Za-z]:\\' -or $ExpectedSHA256 -cnotmatch '^[0-9a-f]{64}$'){
  throw 'Absolute assembly path and lowercase SHA256 required'
 }
 $dll=[IO.File]::Open($AssemblyPath,'Open','Read','Read')
 if($dll.Length -lt 1 -or $dll.Length -gt 1048576){throw 'Unsupported reader assembly size'}
 $memory=New-Object IO.MemoryStream
 try {$dll.CopyTo($memory);$assemblyBytes=$memory.ToArray()} finally {$memory.Dispose()}
 if((Hash-Bytes $assemblyBytes) -cne $ExpectedSHA256){throw 'Fixed reader assembly SHA256 mismatch'}
 $assembly=[Reflection.Assembly]::Load($assemblyBytes)
 $type=$assembly.GetType('PairAuditInput',$true)
 $methods=@($type.GetMethods([Reflection.BindingFlags]'Static,Public,NonPublic,DeclaredOnly') | ForEach-Object {
  $body=$_.GetMethodBody()
  if($body){$il=$body.GetILAsByteArray();@{metadata_token=$_.MetadataToken;name=$_.Name;signature=$_.ToString();il_sha256=(Hash-Bytes $il);il_bytes=$il.Length}}
 })
 if($methods.Count -ne 3){throw 'Managed reader method set mismatch'}
 $arguments=New-Object object[] 1
 $arguments[0]=$LiteralPath
 $bytes=$type.GetMethod('Read').Invoke($null,$arguments)
 $observation=@{schema_version=1;formal=$false;pid=$PID;process_creation_utc=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o');assembly_input_sha256=$ExpectedSHA256;assembly_input_base64=[Convert]::ToBase64String($assemblyBytes);assembly_location=$assembly.Location;mvid=$type.Module.ModuleVersionId.ToString();methods=$methods}
 [Console]::Error.WriteLine('PAIR_FIXED_READER_OBSERVATION:'+($observation | ConvertTo-Json -Depth 4 -Compress))
 $stdout=[Console]::OpenStandardOutput()
 $stdout.Write($bytes,0,$bytes.Length)
 $stdout.Flush()
} catch {
 [Console]::Error.WriteLine('Fixed reader failed: '+$_.Exception.Message)
 exit 1
} finally {
 if($dll){$dll.Dispose()}
 $sha.Dispose()
}
