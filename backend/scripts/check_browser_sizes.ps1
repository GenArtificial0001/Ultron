Get-ChildItem -Directory 'C:\Proyectos\Jarvis con Claude\vision-web\backend\browser' | ForEach-Object {
    $size = (Get-ChildItem $_.FullName -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    [PSCustomObject]@{ Name = $_.Name; SizeMB = [math]::Round($size/1MB,3); SizeBytes = $size }
} | Sort-Object SizeBytes -Descending | Format-Table -AutoSize
