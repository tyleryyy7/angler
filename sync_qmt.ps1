$src = 'D:\angler\executor_v2.py'
$dst = 'D:\国金证券QMT交易端\python\钓鱼.py'
$bakdir = 'D:\国金证券QMT交易端\python\backups'

$bytes = [System.IO.File]::ReadAllBytes($src)
if ($bytes | Where-Object { $_ -ge 128 } | Select-Object -First 1) {
    Write-Host "[ABORT] $src contains non-ASCII bytes. QMT requires pure ASCII source."
    exit 1
}

if (-not (Test-Path $bakdir)) { New-Item -ItemType Directory -Path $bakdir | Out-Null }
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
if (Test-Path $dst) {
    Copy-Item $dst (Join-Path $bakdir "executor_v2_$ts.py")
}
Copy-Item $src $dst -Force
Write-Host "[OK] synced $src -> $dst  (backup: $bakdir\executor_v2_$ts.py)"
Write-Host "NOTE: restart the strategy in QMT client to load the new version."
