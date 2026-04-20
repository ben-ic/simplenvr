"""Ecosystem publication — additive layers that republish SimpleNVR's
cameras to third-party smart-home platforms (Apple Home via HomeKit in
Phase 1; Google Home / Alexa / SmartThings via Matter in Phase 2).

Load-bearing rule (plans/homekit-matter-integration-plan.md §Architecture):
integration is ADDITIVE, never subtractive. A crashed / unpaired / stopped
ecosystem layer never changes whether the recorder writes segments, never
routes recording through a vendor's cloud, and never gates detection. The
recorder is the source of truth; this package publishes, nothing more.
"""
