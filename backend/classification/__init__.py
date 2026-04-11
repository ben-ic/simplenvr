"""Object classification subsystem.

YOLOX-S ONNX detector that upgrades Inbox rows from generic "Motion at X"
to specific "Person at X" / "Vehicle at X" / "Animal at X" labels.

Silent confidence fallback: if the model is not confident, the label stays
NULL and the Inbox sentence stays honest. The classifier can never make the
product worse — it is strictly additive.
"""
