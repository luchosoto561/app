# scripts\gen-tree.ps1  (simple)
$ErrorActionPreference = "Stop"
$outFile = "docs\arbol.txt"

# Crear carpeta docs si no existe
New-Item -ItemType Directory -Force -Path "docs" | Out-Null

# Encabezado con fecha/hora
"Árbol generado: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File $outFile -Encoding UTF8

# Árbol simple (usa el 'tree' de Windows)
tree /A /F | Out-File $outFile -Append -Encoding UTF8

Write-Host "Listo: $outFile"