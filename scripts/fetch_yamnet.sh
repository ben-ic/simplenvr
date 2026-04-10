#!/usr/bin/env bash
# Download YAMNet ONNX model and class names for audio classification.
#
# Outputs:
#   backend/classification/models/yamnet.onnx       (~4 MB)
#   backend/classification/models/yamnet_classes.txt (521 lines, one class per line)
#
# The model is exported from Google's TensorFlow Model Garden YAMNet
# (Apache-2.0). The class names follow the AudioSet ontology (CC-BY 4.0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
MODEL_DIR="$REPO_ROOT/backend/classification/models"

YAMNET_URL="https://github.com/onnx/models/raw/main/validated/vision/body_analysis/emotion_ferplus/model/yamnet.onnx"
# TODO: Replace with the actual hosted URL for the yamnet ONNX export.
# For now, use the tf2onnx export from the TF Model Garden:
#   python -m tf2onnx.convert --saved-model yamnet_saved_model --output yamnet.onnx
# Then host the .onnx file and update this URL + hash.

CLASSES_URL="https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"

mkdir -p "$MODEL_DIR"

# ── Download class names ──
echo "Fetching YAMNet class names..."
CLASSES_FILE="$MODEL_DIR/yamnet_classes.txt"
if [ -f "$CLASSES_FILE" ]; then
    echo "  → yamnet_classes.txt already exists, skipping"
else
    # The CSV has columns: index, mid, display_name
    # We extract just the display_name column (3rd field).
    curl -fsSL "$CLASSES_URL" \
        | tail -n +2 \
        | cut -d',' -f3 \
        | sed 's/^"//; s/"$//' \
        > "$CLASSES_FILE"
    LINES=$(wc -l < "$CLASSES_FILE" | tr -d ' ')
    echo "  → saved $LINES class names to yamnet_classes.txt"
fi

# ── Download ONNX model ──
echo "Fetching YAMNet ONNX model..."
ONNX_FILE="$MODEL_DIR/yamnet.onnx"
if [ -f "$ONNX_FILE" ]; then
    echo "  → yamnet.onnx already exists, skipping"
else
    echo "  ⚠ Automatic download not yet configured."
    echo "  To get the YAMNet ONNX model:"
    echo "    1. pip install tf2onnx tensorflow tensorflow-hub"
    echo "    2. python -c \""
    echo "       import tensorflow_hub as hub"
    echo "       model = hub.load('https://tfhub.dev/google/yamnet/1')"
    echo "       import tf2onnx"
    echo "       tf2onnx.convert.from_function("
    echo "           model.signatures['serving_default'],"
    echo "           input_signature=[tf.TensorSpec([15360], tf.float32, name='waveform')],"
    echo "           output_path='yamnet.onnx'"
    echo "       )\""
    echo "    3. cp yamnet.onnx $ONNX_FILE"
    exit 1
fi

echo "Done. Model files in: $MODEL_DIR"
