"""HomeKit bridge — advertises SimpleNVR cameras into Apple Home.

Topology: bridge mode. One HAP bridge accessory ("SimpleNVR") enumerates
N child accessories (one per online non-hub camera). Users pair the
bridge once; every camera they add afterward appears automatically.

HKSV is NOT advertised by default — see bridge.py.
"""

from .bridge import HomeKitBridge

__all__ = ["HomeKitBridge"]
