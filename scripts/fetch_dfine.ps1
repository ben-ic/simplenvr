# Verify the pinned D-FINE-N ONNX model committed at
# backend/classification/models/dfine_n.onnx.
#
# Usage:
#   scripts\fetch_dfine.ps1
#
# What it does:
#   - The .onnx (15.3 MB) is committed in-tree. This script's job is to
#     verify the committed file has the SHA256 we expect, and to write
#     NOTICE.txt alongside it.
#   - If the file is missing (or its hash changes), the script fails loud.
#
# Re-exporting the model (rare; only when rev'ing D-FINE):
#   1. git clone https://github.com/Peterande/D-FINE (pinned below)
#   2. Download dfine_n_coco.pth (pinned SHA256 below)
#   3. python tools\deployment\export_onnx.py `
#        --config configs\dfine\dfine_hgnetv2_n_coco.yml `
#        --resume <path-to-pth> --check --simplify
#   4. Copy the resulting dfine_n_coco.onnx -> backend\classification\models\dfine_n.onnx
#   5. Update DFINE_ONNX_SHA256 in this script and scripts\fetch_dfine.sh.
#
# License: D-FINE is Apache-2.0 (https://github.com/Peterande/D-FINE).

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$RepoRoot  = Resolve-Path (Join-Path $PSScriptRoot '..')
$ModelDir  = Join-Path $RepoRoot 'backend\classification\models'
$OnnxFile  = Join-Path $ModelDir 'dfine_n.onnx'

# --- Pinned upstream + checksums -------------------------------------------

$DFineRelease   = 'dfinev1.0'
$DFinePthUrl    = "https://github.com/Peterande/storage/releases/download/$DFineRelease/dfine_n_coco.pth"
$DFinePthSha256 = '41973938d2784d38a9836990d805b8392855ebf611aba55f0f7add90e110744c'

$DFineOnnxSha256 = '2bf2775b175b45ab582e4777f4fab75923a86cb5e2d3824f2735c2b25f1abc8d'

# --- Helpers ---------------------------------------------------------------

function Log($msg) { Write-Host "[fetch_dfine] $msg" }
function Die($msg) { Write-Error "[fetch_dfine] ERROR: $msg"; exit 1 }

function Sha256-Of($Path) {
    return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLower()
}

# --- Main ------------------------------------------------------------------

if (-not (Test-Path $ModelDir)) {
    New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
}

if (-not (Test-Path $OnnxFile)) {
    Die "$OnnxFile is missing. This model ships committed in-tree. If you hit this during a re-export, see the header of this script for the export procedure."
}

$actual = Sha256-Of $OnnxFile
if ($actual -ne $DFineOnnxSha256.ToLower()) {
    Die "dfine_n.onnx SHA256 mismatch: expected $DFineOnnxSha256, got $actual. Either the committed weights drifted or the pin is stale. Do NOT update the pin until you have re-run the eyeball test on real frames."
}
Log "dfine_n.onnx SHA256 OK"

# Write/refresh NOTICE.txt. This is the single source of truth for the
# entire model directory's attribution — D-FINE (vision), YAMNet (audio),
# and the AudioSet class map (CC-BY-4.0). scripts\fetch_yamnet.ps1
# deliberately does not touch NOTICE.txt so the two scripts can't race.
# Use a here-string with no interpolation.
$notice = @'
D-FINE
Copyright (c) 2024 Yansong Peng.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Source: https://github.com/Peterande/D-FINE
Release: dfinev1.0
Checkpoint: dfine_n_coco.pth
Exported to ONNX with tools/deployment/export_onnx.py (--simplify).

-------------------------------------------------------------------------------

YAMNet
Copyright 2019 The TensorFlow Authors. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Source: https://github.com/tensorflow/models/tree/master/research/audioset/yamnet
TF-Hub: https://tfhub.dev/google/yamnet/1
Exported to ONNX with tf2onnx from the TF-Hub SavedModel.

-------------------------------------------------------------------------------

AudioSet Ontology / Class Map (yamnet_classes.txt)
Copyright (c) Google LLC.

The AudioSet class ontology and the per-class display names used in
yamnet_classes.txt are released by Google under the
Creative Commons Attribution 4.0 International License (CC-BY-4.0).

    https://creativecommons.org/licenses/by/4.0/

Source: https://research.google.com/audioset/
Class map: https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv
yamnet_classes.txt is the display_name column (3rd CSV field) of that map,
one class per line, no other modifications.
'@

Set-Content -Path (Join-Path $ModelDir 'NOTICE.txt') -Value $notice -Encoding ASCII

Log "done. Models in ${ModelDir}:"
Get-ChildItem $ModelDir | Format-Table Name, Length
