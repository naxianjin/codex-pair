$ErrorActionPreference='Stop'
$env:PSModulePath=[Environment]::GetEnvironmentVariable('PSModulePath','Machine')
Import-Module Microsoft.PowerShell.Utility
$lab='E:\codex-pair\.codex-pair\isolation';$vmName='codex-pair-isolation-20261102'
if(Test-Path "$lab\vm-pending.json"){throw 'Pending execution preserved'}
$lease=[IO.File]::Open("$lab\vm-execution.lock",'OpenOrCreate','ReadWrite','None');$s=$null
try {
Start-VM -Name $vmName
Start-Sleep -Seconds 120
$s=New-PSSession -VMName $vmName -Credential (Import-Clixml "$lab\windows-20261102\guest-credential.dpapi.xml")
$result=Invoke-Command -Session $s -ScriptBlock {
 $ErrorActionPreference='Stop'
 $file='C:\Users\Administrator\AppData\Local\Temp\pair-validator-b970d7ae4dfb42f994e311eda76604ca.etl'
 if(-not(Test-Path -LiteralPath $file)){
  $file=(Get-ChildItem -Path C:\Users,C:\Windows\Temp -Filter '*b970d7ae4dfb42f994e311eda76604ca.etl' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
 }
 if(-not $file){throw 'Original ETL not found'}
 $provider=Get-WinEvent -ListProvider Microsoft-Windows-DotNETRuntime
 $templates=@($provider.Events | Where-Object {$_.Id -in 152,153 -and $_.Version -eq 2} | Select-Object Id,Version,Template)
 $events=@(Get-WinEvent -Path $file -Oldest | Where-Object {$_.ProviderName -eq 'Microsoft-Windows-DotNETRuntime' -and $_.Id -in 152,153} | ForEach-Object {
  @{Id=$_.Id;Version=$_.Version;PID=$_.ProcessId;Properties=@($_.Properties | ForEach-Object {if($null -eq $_.Value){$null}else{$_.Value.ToString()}})}
 })
 @{Formal=$false;Templates=$templates;Events=$events} | ConvertTo-Json -Depth 8
}
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'guest-clr-raw-properties.json'),($result -join "`n"))
Write-Output 'Raw CLR properties saved'
}finally{if($s){Remove-PSSession $s};Stop-VM -Name $vmName -TurnOff -Confirm:$false;$lease.Dispose()}
