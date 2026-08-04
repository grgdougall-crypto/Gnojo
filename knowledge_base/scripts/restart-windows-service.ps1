[CmdletBinding(SupportsShouldProcess=$true, ConfirmImpact="High")]
param([Parameter(Mandatory=$true)][ValidateNotNullOrEmpty()][string]$ServiceName)
$ErrorActionPreference = "Stop"
$service = Get-Service -Name $ServiceName -ErrorAction Stop
$service | Select-Object Status, Name, DisplayName | Format-Table -AutoSize
if ($PSCmdlet.ShouldProcess("$($service.DisplayName) [$($service.Name)]", "Restart Windows service")) {
    Restart-Service -Name $service.Name -ErrorAction Stop
    $service.WaitForStatus("Running", [TimeSpan]::FromSeconds(30))
    Get-Service -Name $service.Name | Select-Object Status, Name, DisplayName | Format-Table -AutoSize
}
