# Замер ресурсов SupplyBro (Docker). Запуск: см. инструкцию в чате.
param([int]$Minutes = 10, [int]$Interval = 5)

function ToMB($s) {
  $s = $s.Trim()
  if ($s -match '^([\d\.]+)\s*(B|KiB|MiB|GiB|kB|MB|GB)$') {
    $v = [double]::Parse($matches[1], [Globalization.CultureInfo]::InvariantCulture)
    switch ($matches[2]) { 'B' {$v/1MB} 'KiB' {$v/1024} 'kB' {$v/1024} 'MiB' {$v} 'MB' {$v} 'GiB' {$v*1024} 'GB' {$v*1024} }
  } else { 0 }
}

$D = 'Ubuntu-24.04'
$out = Join-Path $PSScriptRoot 'замер_результат.txt'
$csv = Join-Path $PSScriptRoot 'замер_данные.csv'
"time;container;cpu_percent;mem_mb" | Out-File $csv -Encoding utf8

Write-Host "Замер идет $Minutes мин. Пока он идет, ПОЛЬЗУЙТЕСЬ сайтом как обычно:" -ForegroundColor Yellow
Write-Host "загрузите конкурс, запустите анализ, чат, поиск цен." -ForegroundColor Yellow

$data = @{}
$total = @()
$end = (Get-Date).AddMinutes($Minutes)
while ((Get-Date) -lt $end) {
  $lines = wsl -d $D -e docker stats --no-stream --format "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}" 2>$null
  $sumMem = 0; $sumCpu = 0
  foreach ($l in $lines) {
    $p = $l -split '\|'
    if ($p.Count -lt 3) { continue }
    $cpu = [double]::Parse(($p[1] -replace '%',''), [Globalization.CultureInfo]::InvariantCulture)
    $mem = ToMB (($p[2] -split '/')[0])
    if (-not $data.ContainsKey($p[0])) { $data[$p[0]] = @() }
    $data[$p[0]] += ,@($cpu, $mem)
    $sumMem += $mem; $sumCpu += $cpu
    ("{0};{1};{2};{3}" -f (Get-Date -Format 'HH:mm:ss'), $p[0], $cpu, [math]::Round($mem,1)) | Out-File $csv -Append -Encoding utf8
  }
  $total += ,@($sumCpu, $sumMem)
  Write-Host ("{0}  CPU: {1,6:N1}%   RAM: {2,7:N0} МБ" -f (Get-Date -Format 'HH:mm:ss'), $sumCpu, $sumMem)
  Start-Sleep -Seconds $Interval
}

$r = @()
$r += "=== ИТОГ ЗАМЕРА SupplyBro ($Minutes мин) ==="
foreach ($k in $data.Keys) {
  $cpus = $data[$k] | ForEach-Object { $_[0] }
  $mems = $data[$k] | ForEach-Object { $_[1] }
  $r += ("{0}: RAM средняя {1:N0} МБ, ПИК {2:N0} МБ | CPU средний {3:N1}%, пик {4:N1}%" -f $k, ($mems | Measure-Object -Average).Average, ($mems | Measure-Object -Maximum).Maximum, ($cpus | Measure-Object -Average).Average, ($cpus | Measure-Object -Maximum).Maximum)
}
$tm = $total | ForEach-Object { $_[1] }
$tc = $total | ForEach-Object { $_[0] }
$r += ("ВСЕГО: RAM средняя {0:N0} МБ, ПИК {1:N0} МБ | CPU (100% = одно ядро) средний {2:N1}%, пик {3:N1}%" -f ($tm | Measure-Object -Average).Average, ($tm | Measure-Object -Maximum).Maximum, ($tc | Measure-Object -Average).Average, ($tc | Measure-Object -Maximum).Maximum)

$r += ""
$r += "=== ДИСК ==="
$sz = wsl -d $D -e docker image inspect supplybro-web:dev --format '{{.Size}}' 2>$null
if ($sz) { $r += ("Образ Docker: {0:N0} МБ" -f ([double]$sz/1MB)) }
foreach ($key in 'DATA_DIR','TENDERS_DIR') {
  $m = Select-String -Path (Join-Path $PSScriptRoot '..\.env') -Pattern ("^\s*" + $key + "\s*=\s*(.+)$") | Select-Object -First 1
  if ($m) {
    $path = $m.Matches[0].Groups[1].Value.Trim().Trim('"')
    $du = wsl -d $D -e du -sm $path 2>$null
    if ($du) { $r += ("{0}: {1} МБ" -f $key, ($du -split '\s+')[0]) }
  }
}

$r | Tee-Object -FilePath $out
Write-Host "`nРезультат сохранен: $out" -ForegroundColor Green
