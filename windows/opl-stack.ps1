<#
.SYNOPSIS
  Start, check or stop the whole local stack after a Windows restart:
  OpenProject (Docker in one WSL distribution) and the conductor (in the
  Spark sandbox distribution).

.DESCRIPTION
  Manual, not an autostart: run it yourself, or let an LLM run it. It never
  prompts, is safe to run again (anything already running is left alone),
  prints one [opl-stack] line per step, never prints keys, and exits 0 only
  when everything it was asked for is up.

  start   1. boots the OpenProject distribution (its containers restart on
             their own), waits for the health check;
          2. if still unhealthy and -OpenProjectToolkit is given, runs
             bin/opl-start there and waits again;
          3. starts the conductor in the sandbox distribution with
             bin/opl-conductor-start (live unless -WatchOnly; live mode
             still also needs live = true in opl.toml).
  status  OpenProject health and bin/opl-conductor-start status.
  stop    stops the conductor only (OpenProject keeps running; stop it with
          bin/opl-stop in its distribution).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File windows\opl-stack.ps1 start -OpenProjectToolkit /mnt/c/path/to/openproject-local-pilot -NodeBin ~/opl/node/bin
#>
param(
  [ValidateSet('start', 'status', 'stop')]
  [string]$Action = 'status',
  [string]$OpenProjectDistro = 'Ubuntu-24.04',
  [string]$OpenProjectUrl = 'http://localhost:8080',
  # WSL path of a toolkit checkout inside the OpenProject distribution; only
  # used when the containers do not come back on their own.
  [string]$OpenProjectToolkit = '',
  [string]$ConductorDistro = 'opl-sandbox',
  # Toolkit checkout inside the sandbox distribution, relative to the home
  # directory of its default user.
  [string]$ConductorToolkit = 'opl/toolkit',
  # Extra PATH entry for the conductor, e.g. ~/opl/node/bin for cost reports.
  [string]$NodeBin = '',
  [switch]$WatchOnly,
  [int]$HealthTimeoutSec = 600
)

$ErrorActionPreference = 'Stop'

function Say([string]$Text) { Write-Output "[opl-stack] $Text" }

function Test-Health {
  try {
    $r = Invoke-WebRequest -Uri "$($OpenProjectUrl.TrimEnd('/'))/health_checks/default" `
      -UseBasicParsing -TimeoutSec 10
    return $r.StatusCode -eq 200
  } catch {
    return $false
  }
}

function Wait-Health([int]$Seconds) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-Health) { return $true }
    Start-Sleep -Seconds 5
  }
  return (Test-Health)
}

function Invoke-Conductor([string[]]$Arguments) {
  # --cd ~ plus a relative path: resolved against the distribution's own
  # home directory, executed directly (no shell, so nothing is re-parsed).
  # Out-Host: show its lines without making them part of the return value.
  & wsl.exe -d $ConductorDistro --cd '~' -- "$ConductorToolkit/bin/opl-conductor-start" @Arguments | Out-Host
  return $LASTEXITCODE
}

switch ($Action) {
  'status' {
    if (Test-Health) { Say "OpenProject healthy at $OpenProjectUrl" }
    else { Say "OpenProject NOT reachable at $OpenProjectUrl" }
    $code = Invoke-Conductor @('status')
    if ((Test-Health) -and $code -eq 0) { Say 'stack up'; exit 0 }
    Say 'stack not fully up'
    exit 1
  }

  'stop' {
    $code = Invoke-Conductor @('stop')
    if ($code -ne 0) { Say "conductor stop failed (exit $code)"; exit 1 }
    Say 'conductor stopped; OpenProject left running'
    exit 0
  }

  'start' {
    if (Test-Health) {
      Say "OpenProject already healthy at $OpenProjectUrl"
    } else {
      Say "booting WSL distribution $OpenProjectDistro (containers restart on their own)"
      & wsl.exe -d $OpenProjectDistro -- true
      if ($LASTEXITCODE -ne 0) { Say "cannot start $OpenProjectDistro"; exit 1 }
      $first = [Math]::Min(180, $HealthTimeoutSec)
      Say "waiting up to $first s for OpenProject"
      $healthy = Wait-Health $first
      if (-not $healthy -and $OpenProjectToolkit) {
        Say "not healthy yet; running bin/opl-start in $OpenProjectToolkit"
        # bash -i so the distribution's ~/.bashrc OPL_* settings apply.
        & wsl.exe -d $OpenProjectDistro --cd $OpenProjectToolkit -- bash -ic bin/opl-start
        $healthy = Wait-Health ([Math]::Max(0, $HealthTimeoutSec - $first))
      }
      if (-not $healthy) {
        Say "OpenProject did not become healthy at $OpenProjectUrl"
        Say "check it inside ${OpenProjectDistro}: bin/opl-status and bin/opl-logs web"
        exit 1
      }
      Say "OpenProject healthy at $OpenProjectUrl"
    }

    # --supervise: restart a dead or stalled conductor (#48, #60).
    $conductorArgs = @('start', '--supervise')
    if (-not $WatchOnly) { $conductorArgs += '--live' }
    if ($NodeBin) { $conductorArgs += @('--path', $NodeBin) }
    Say "starting the conductor in $ConductorDistro"
    $code = Invoke-Conductor $conductorArgs
    if ($code -ne 0) { Say "conductor did not start (exit $code)"; exit 1 }
    Say "stack up: open $OpenProjectUrl"
    exit 0
  }
}
