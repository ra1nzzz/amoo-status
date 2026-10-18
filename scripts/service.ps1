param(
    [ValidateSet('Install', 'Start', 'Stop', 'Restart', 'Status', 'Uninstall')]
    [string]$Action = 'Status',
    [string]$TaskName = 'AmooStatus',
    [string]$Repo = 'ra1nzzz/amoo-status',
    [string]$Branch = 'main'
)
$ErrorActionPreference = 'Stop'
$projectPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$entryPath = Join-Path $projectPath 'statusd.py'
$runtimePath = Join-Path $projectPath '.runtime'
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask -and -not ($existingTask.Actions.Arguments | Where-Object { $_.Contains($entryPath) })) {
    throw "Task '$TaskName' belongs to another installation. Choose another TaskName."
}

switch ($Action) {
    'Install' {
        $pythonPath = (Get-Command python -ErrorAction Stop).Source
        $pythonWindowless = Join-Path (Split-Path $pythonPath -Parent) 'pythonw.exe'
        if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'pythonw.exe was not found beside python.exe.' }
        if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'GitHub CLI (gh) is required.' }
        $codexPath = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
        $arguments = '"{0}" run --publish --repo "{1}" --branch "{2}" --codex-home "{3}" --runtime-dir "{4}" --output "{5}"' -f `
            $entryPath, $Repo, $Branch, $codexPath, $runtimePath, (Join-Path $projectPath 'status.json')
        $taskAction = New-ScheduledTaskAction -Execute $pythonWindowless -Argument $arguments -WorkingDirectory $projectPath
        $accountName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $accountName
        # RestartOnFailure does not recover every externally terminated process.
        $watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes 1)
        $principal = New-ScheduledTaskPrincipal -UserId $accountName -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        if ($existingTask) { Stop-ScheduledTask -TaskName $TaskName }
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger @($logonTrigger, $watchdogTrigger) `
            -Principal $principal -Settings $settings -Description 'Collect local agent state and publish status.json.' -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
    }
    'Start' {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
    }
    'Stop' {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Stop-ScheduledTask -TaskName $TaskName
    }
    'Restart' {
        Stop-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 1
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
    }
    'Uninstall' {
        if ($existingTask) {
            Stop-ScheduledTask -TaskName $TaskName
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
}
Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Select-Object TaskName, State
$statePath = Join-Path $runtimePath 'service.json'
if (Test-Path -LiteralPath $statePath) { Get-Content -LiteralPath $statePath -Raw }
