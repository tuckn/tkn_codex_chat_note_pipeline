# Register a daily CLI trigger; the CLI decides which calendar reviews are due.
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$CliPath,
    [Parameter(Mandatory)][string]$ConfigPath,
    [Parameter(Mandatory)][string]$WorkingDirectory,
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')][string]$At = '03:00',
    [string]$TaskName = 'TknCodexChatReviews'
)
$ErrorActionPreference = 'Stop'
$resolvedCli = (Resolve-Path -LiteralPath $CliPath).Path
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$resolvedWorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).Path
if (-not (Test-Path -LiteralPath $resolvedCli -PathType Leaf) -or
    -not (Test-Path -LiteralPath $resolvedConfig -PathType Leaf) -or
    -not (Test-Path -LiteralPath $resolvedWorkingDirectory -PathType Container)) {
    throw 'CLI/config must be files and WorkingDirectory must be a directory.'
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Task already exists: $TaskName. Inspect it before changing its definition."
}
$taskArguments = '--config "{0}" scheduled-run' -f $resolvedConfig
$triggerTime = [datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
$action = New-ScheduledTaskAction -Execute $resolvedCli -Argument $taskArguments -WorkingDirectory $resolvedWorkingDirectory
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12)
if ($PSCmdlet.ShouldProcess($TaskName, "Register daily review task at $At; $resolvedCli $taskArguments")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Preserve Codex Raw and generate closed weekly, monthly and yearly reviews.'
}
