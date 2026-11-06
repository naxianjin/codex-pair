$ErrorActionPreference='Stop'
$env:PSModulePath=[Environment]::GetEnvironmentVariable('PSModulePath','Machine')
$vmName='codex-pair-isolation-20261102'
try {
 Start-VM -Name $vmName
 Start-Sleep -Seconds 160
 & 'D:\Codex_No_Projects\work\github\codex-pair\.codex-pair\worktrees\next-recovery-20261101\validation\bytecode-provenance-20261105\Invoke-ValidatorRawModuleDiagnostic.ps1' -SourceInventory (Join-Path $PSScriptRoot 'raw-module-source-inventory.json') -InputDirectory 'D:\Codex_No_Projects\work\github\codex-pair-real-wokey-20261104\.codex-pair\worktrees\windows-f93e3110d73b-a\validation\direct-tests-v4\hyperv-validator\store-bound-input'
} finally {Stop-VM -Name $vmName -TurnOff -Confirm:$false}
