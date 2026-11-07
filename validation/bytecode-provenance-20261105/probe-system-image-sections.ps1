param([Parameter(Mandatory=$true)][string]$CaptureDirectory,
      [Parameter(Mandatory=$true)][string]$RuntimeSymbolsPath)
$ErrorActionPreference='Stop'
$sha=[Security.Cryptography.SHA256]::Create()
function Hash-Bytes([byte[]]$bytes){[BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()}
function Check-Range([long]$start,[long]$length,[long]$limit){
 if($start -lt 0 -or $length -lt 0 -or $start -gt $limit -or $length -gt $limit-$start){throw 'PE range outside captured image'}
}
function Read-State($module,$symbols){
 @(foreach($symbol in $symbols){
  if($symbol.resolved -ne $true -or $symbol.rva -isnot [int] -or $symbol.name -isnot [string]){throw 'Invalid runtime symbol'}
  Check-Range $symbol.rva 128 $module.ModuleMemorySize
  $bytes=New-Object byte[] 128
  [Runtime.InteropServices.Marshal]::Copy([IntPtr]::Add($module.BaseAddress,$symbol.rva),$bytes,0,128)
  @{Name=$symbol.name;RVA=$symbol.rva;MemoryHex=[BitConverter]::ToString($bytes).Replace('-','').ToLowerInvariant()}
 })
}
$locks=New-Object 'System.Collections.Generic.List[System.IDisposable]'
try {
 if(Test-Path -LiteralPath $CaptureDirectory){throw 'Capture directory already exists'}
 [IO.Directory]::CreateDirectory([IO.Path]::GetFullPath($CaptureDirectory)) | Out-Null
 $contractLock=[IO.File]::Open($RuntimeSymbolsPath,'Open','Read','Read');$locks.Add($contractLock)
 $contractMemory=New-Object IO.MemoryStream
 try {$contractLock.CopyTo($contractMemory);$contractBytes=$contractMemory.ToArray()}finally{$contractMemory.Dispose()}
 $contract=[Text.Encoding]::UTF8.GetString($contractBytes) | ConvertFrom-Json
 if($contract.formal -ne $false -or $contract.schema_version -ne 1 -or @($contract.symbols).Count -ne 28){throw 'Invalid runtime contract'}
 $capture=[guid]::NewGuid().ToString('N')
 $created=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o')
 $contractHash=Hash-Bytes $contractBytes
 $collectorHash=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
 $modules=@(Get-Process -Id $PID -Module | Where-Object {$_.ModuleName.ToLowerInvariant() -in @('ntdll.dll','kernel32.dll','clr.dll')})
 if($modules.Count -ne 3){throw 'Expected three distinct system images'}
 $images=@(foreach($module in $modules){
  $stream=[IO.File]::Open($module.FileName,'Open','Read','Read');$locks.Add($stream)
  $memory=New-Object IO.MemoryStream
  try {$stream.CopyTo($memory);$disk=$memory.ToArray()}finally{$memory.Dispose()}
  Check-Range 0 64 $disk.Length
  if([BitConverter]::ToUInt16($disk,0) -ne 23117){throw 'Invalid DOS signature'}
  $pe=[BitConverter]::ToInt32($disk,60);Check-Range $pe 24 $disk.Length
  if([BitConverter]::ToUInt32($disk,$pe) -ne 17744 -or [BitConverter]::ToUInt16($disk,$pe+4) -ne 34404){throw 'Expected AMD64 PE image'}
  $count=[BitConverter]::ToUInt16($disk,$pe+6);$optional=$pe+24
  $optionalSize=[BitConverter]::ToUInt16($disk,$pe+20);Check-Range $optional $optionalSize $disk.Length
  if($count -lt 1 -or $count -gt 96 -or $optionalSize -lt 160 -or [BitConverter]::ToUInt16($disk,$optional) -ne 523){throw 'Invalid PE32+ optional header'}
  if([BitConverter]::ToUInt32($disk,$optional+108) -lt 6){throw 'Missing relocation directory'}
  $imageSize=[BitConverter]::ToUInt32($disk,$optional+56)
  if($imageSize -ne $module.ModuleMemorySize){throw 'Loaded PE image size mismatch'}
  $table=$optional+$optionalSize;Check-Range $table (40*$count) $disk.Length
  $headers=@(for($i=0;$i -lt $count;$i++){
   $o=$table+40*$i;$name=[Text.Encoding]::ASCII.GetString($disk,$o,8).TrimEnd([char]0)
   if($name -notmatch '^[.A-Za-z0-9_]{1,8}$'){throw 'Unsupported section name'}
   $size=[BitConverter]::ToUInt32($disk,$o+8);$rva=[BitConverter]::ToUInt32($disk,$o+12)
   $rawSize=[BitConverter]::ToUInt32($disk,$o+16);$raw=[BitConverter]::ToUInt32($disk,$o+20)
   Check-Range $rva $size $imageSize;Check-Range $raw $rawSize $disk.Length
   [pscustomobject]@{Name=$name;Size=$size;RVA=$rva;RawSize=$rawSize;Raw=$raw;Flags=[BitConverter]::ToUInt32($disk,$o+36)}
  })
  if(@($headers.Name | Sort-Object -Unique).Count -ne $count){throw 'Duplicate PE sections'}
  $ordered=@($headers | Sort-Object RVA)
  for($i=1;$i -lt $ordered.Count;$i++){if($ordered[$i].RVA -lt ($ordered[$i-1].RVA+$ordered[$i-1].Size)){throw 'Overlapping PE sections'}}
  $relocRva=[BitConverter]::ToUInt32($disk,$optional+152);$relocSize=[BitConverter]::ToUInt32($disk,$optional+156)
  $relocations=New-Object 'System.Collections.Generic.HashSet[long]'
  if($relocSize){
   $owners=@($headers | Where-Object {$relocRva -ge $_.RVA -and $relocRva+$relocSize -le $_.RVA+$_.RawSize})
   if($owners.Count -ne 1){throw 'Relocation directory not fully backed by one section'}
   $cursor=$owners[0].Raw+$relocRva-$owners[0].RVA;$end=$cursor+$relocSize;Check-Range $cursor $relocSize $disk.Length
   while($cursor -lt $end){
    Check-Range $cursor 8 $end
    $page=[BitConverter]::ToUInt32($disk,$cursor);$block=[BitConverter]::ToUInt32($disk,$cursor+4)
    if($block -lt 8 -or $block%2 -ne 0){throw 'Invalid relocation block'}
    Check-Range $cursor $block $end
    for($k=$cursor+8;$k -lt $cursor+$block;$k+=2){
     $entry=[BitConverter]::ToUInt16($disk,$k);$kind=$entry -shr 12
     if($kind -eq 10){$target=[long]$page+($entry -band 4095);Check-Range $target 8 $imageSize;if(-not $relocations.Add($target)){throw 'Duplicate relocation'}}elseif($kind -ne 0){throw 'Unsupported relocation type'}
    }
    $cursor+=$block
   }
  }
  $delta=$module.BaseAddress.ToInt64()-[long][BitConverter]::ToUInt64($disk,$optional+24)
  $diskHash=Hash-Bytes $disk
  if($module.ModuleName -eq 'clr.dll' -and $diskHash -ne $contract.clr_sha256){throw 'CLR runtime contract build mismatch'}
  $executable=@($headers | Where-Object {($_.Flags -band 0x20000000) -ne 0})
  $imageContract=$contract.images.($module.ModuleName.ToLowerInvariant())
  if(-not $imageContract -or $imageContract.DiskSHA256 -ne $diskHash -or $imageContract.ImageSize -ne $imageSize -or @($imageContract.Sections).Count -ne $executable.Count){throw 'System image contract mismatch'}
  foreach($section in $executable){
   $fixed=@($imageContract.Sections | Where-Object Section -eq $section.Name)
   if($fixed.Count -ne 1 -or $fixed[0].RVA -ne $section.RVA -or $fixed[0].VirtualSize -ne $section.Size -or $fixed[0].RawSize -ne $section.RawSize -or $fixed[0].Flags -ne $section.Flags){throw 'Executable section contract mismatch'}
  }
  if(@($executable | Where-Object {($_.Flags -band 0x80000000L) -ne 0}).Count){throw 'Writable executable section unsupported'}
  $sections=@(foreach($section in $executable){
   $name=$section.Name;$rva=$section.RVA;$length=$section.Size
   if($length -eq 0){throw 'Empty executable section'}
   $expected=New-Object byte[] $length
   [Array]::Copy($disk,$section.Raw,$expected,0,[Math]::Min($length,$section.RawSize))
   foreach($relocation in $relocations){
    if($relocation -lt $rva+$length -and $relocation+8 -gt $rva){
     Check-Range ($relocation-$rva) 8 $length
     $position=$relocation-$rva;$value=[BitConverter]::ToInt64($expected,$position)+$delta
     [Array]::Copy([BitConverter]::GetBytes($value),0,$expected,$position,8)
    }
   }
   $mapped=New-Object byte[] $length
   if($module.ModuleName -eq 'clr.dll' -and $name -eq '.text'){$before=Read-State $module $contract.symbols}
   [Runtime.InteropServices.Marshal]::Copy([IntPtr]::Add($module.BaseAddress,$rva),$mapped,0,$length)
   if($module.ModuleName -eq 'clr.dll' -and $name -eq '.text'){
    $state=Read-State $module $contract.symbols
    foreach($global in 'g_card_table','g_ephemeral_low','gThreadTLSIndex','gAppDomainTLSIndex','CExecutionEngine::TlsIndex'){
     $a=@($before | Where-Object Name -eq $global);$b=@($state | Where-Object Name -eq $global)
     $chars=if($global -like 'g_*'){16}else{8}
     if($a.Count -ne 1 -or $b.Count -ne 1 -or $a[0].MemoryHex.Substring(0,$chars) -ne $b[0].MemoryHex.Substring(0,$chars)){throw 'Runtime state changed during section capture'}
    }
    @{SchemaVersion=1;Formal=$false;CaptureId=$capture;PID=$PID;ProcessCreationUTC=$created;RuntimeSymbolsSHA256=$contractHash;CLRSHA256=$diskHash;Symbols=$state} | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $CaptureDirectory 'runtime-state.json')
   }
   [IO.File]::WriteAllBytes((Join-Path $CaptureDirectory ($module.ModuleName+'.'+$name+'.expected.bin')),$expected)
   [IO.File]::WriteAllBytes((Join-Path $CaptureDirectory ($module.ModuleName+'.'+$name+'.actual.bin')),$mapped)
   $diff=@(for($j=0;$j -lt $length;$j++){if($mapped[$j] -ne $expected[$j]){$j}})
   @{Section=$name;RVA=$rva;VirtualSize=$length;RawSize=$section.RawSize;Flags=$section.Flags;ComparedBytes=$length;DifferentBytes=$diff.Count;MappedSHA256=(Hash-Bytes $mapped);DiskSHA256=(Hash-Bytes $expected)}
  })
  @{Name=$module.ModuleName;Path=$module.FileName;DiskSHA256=$diskHash;BaseAddress=$module.BaseAddress.ToInt64().ToString();ImageSize=$imageSize;FileVersion=$module.FileVersionInfo.FileVersion;ExecutableSectionsTotal=$executable.Count;WritableExecutableSections=0;Sections=$sections}
 })
 @{SchemaVersion=1;Formal=$false;CaptureId=$capture;PID=$PID;ProcessCreationUTC=$created;CollectorSHA256=$collectorHash;RuntimeSymbolsSHA256=$contractHash;Images=$images;Limit='Current-process executable virtual sections only; runtime transformations establish consistency, not independent authentication or execution-time coverage.'} | ConvertTo-Json -Depth 7
} finally {foreach($stream in $locks){$stream.Dispose()};$sha.Dispose()}
