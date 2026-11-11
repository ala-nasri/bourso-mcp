<#
.SYNOPSIS
Enregistre (ou supprime) la tâche planifiée quotidienne de synchro bourso-mcp.

.DESCRIPTION
Crée la tâche « bourso-mcp sync », qui lance chaque jour
`uv --directory <racine du dépôt> run bourso-mcp sync --non-interactive`.

La tâche s'exécute sous l'utilisateur courant, seulement quand il est connecté :
elle peut ainsi lire l'entrée `bourso-mcp` du Gestionnaire d'identification et
l'état de session woob chiffré avec DPAPI. Aucun mot de passe n'est enregistré
dans la tâche. Relancer le script met la tâche à jour.

.PARAMETER At
Heure d'exécution quotidienne, au format HH:mm. Par défaut : 07:30.

.PARAMETER Unregister
Supprime la tâche au lieu de l'enregistrer.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/register-sync-task.ps1

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/register-sync-task.ps1 -At "06:45"

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/register-sync-task.ps1 -Unregister
#>
param(
    [string]$At = "07:30",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "bourso-mcp sync"
$root = Split-Path $PSScriptRoot -Parent

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Tâche « $TaskName » supprimée."
    } else {
        Write-Host "Aucune tâche « $TaskName » à supprimer."
    }
    exit 0
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Error "uv est introuvable dans le PATH. Installer uv (winget install --id=astral-sh.uv -e), puis relancer ce script."
}

$action = New-ScheduledTaskAction -Execute $uv.Source `
    -Argument "--directory `"$root`" run bourso-mcp sync --non-interactive" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

$next = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
Write-Host "Tâche « $TaskName » enregistrée : synchro quotidienne à $At, prochaine exécution le $next."
Write-Host "Elle ne s'exécute que lorsque la session Windows est ouverte."
