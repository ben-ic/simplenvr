# Verify the pinned YAMNet ONNX model + AudioSet class map committed at
#   backend\classification\models\yamnet.onnx
#   backend\classification\models\yamnet_classes.txt
#
# Usage:
#   scripts\fetch_yamnet.ps1
#
# What it does:
#   - Both files are committed in-tree. This script's job is to verify
#     the committed files have the SHA256 we expect.
#   - If a file is missing (or its hash changes), the script fails loud.
#   - The model directory's top-level NOTICE.txt is written by
#     scripts\fetch_dfine.ps1 (which owns the heredoc covering D-FINE,
#     YAMNet, and the AudioSet class map).
#
# Re-exporting the model (rare; only when rev'ing YAMNet):
#   1. pip install tf2onnx tensorflow tensorflow-hub
#   2. Export yamnet.onnx via tf2onnx from https://tfhub.dev/google/yamnet/1
#   3. Re-fetch yamnet_classes.txt from
#      https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv
#      (column 3, skip header).
#   4. Update YamnetOnnxSha256 and YamnetClassesSha256 in this script and
#      scripts\fetch_yamnet.sh.
#
# License:
#   YAMNet is Apache-2.0 (Google TensorFlow Model Garden). AudioSet class
#   map is CC-BY-4.0 (Google). NOTICE entries live in the shared
#   backend\classification\models\NOTICE.txt written by fetch_dfine.ps1.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$RepoRoot    = Resolve-Path (Join-Path $PSScriptRoot '..')
$ModelDir    = Join-Path $RepoRoot 'backend\classification\models'
$OnnxFile    = Join-Path $ModelDir 'yamnet.onnx'
$ClassesFile = Join-Path $ModelDir 'yamnet_classes.txt'

# --- Pinned checksums ------------------------------------------------------

$YamnetOnnxSha256    = '22fca37f60dea142179487814c9faa9c2e535497f87a46f6fc0f6df1c20beef8'
$YamnetClassesSha256 = 'a6984f0f8bba8c6f2bd24ee96aa9e488256bf36253c26b7f047c29c62728b026'

# --- Helpers ---------------------------------------------------------------

function Log($msg) { Write-Host "[fetch_yamnet] $msg" }
function Die($msg) { Write-Error "[fetch_yamnet] ERROR: $msg"; exit 1 }

function Sha256-Of($Path) {
    return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLower()
}

function Verify-File($Path, $Expected, $Label) {
    if (-not (Test-Path $Path)) {
        Die "$Path is missing. This file ships committed in-tree. If you hit this during a re-export, see the header of this script for the export procedure."
    }
    $actual = Sha256-Of $Path
    if ($actual -ne $Expected.ToLower()) {
        Die "$Label SHA256 mismatch: expected $Expected, got $actual. Either the committed file drifted or the pin is stale. Do NOT update the pin until you have re-validated detections on real audio."
    }
    Log "$Label SHA256 OK"
}

# --- Main ------------------------------------------------------------------

if (-not (Test-Path $ModelDir)) {
    New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
}

Verify-File $OnnxFile    $YamnetOnnxSha256    'yamnet.onnx'
Verify-File $ClassesFile $YamnetClassesSha256 'yamnet_classes.txt'

Log "done. Models in ${ModelDir}:"
Get-ChildItem $ModelDir | Format-Table Name, Length
