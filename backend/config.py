from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "simplenvr.db"
RECORDINGS_DIR = DATA_DIR / "recordings"

SCAN_INTERVAL = 30.0  # seconds between discovery scans
PROBE_TIMEOUT = 5.0   # seconds to wait for WS-Discovery responses
SERVER_PORT = 8000

# Recording settings
JANITOR_INTERVAL = 60.0  # seconds — periodic janitor safety net
FFMPEG_RESTART_BACKOFF = [5, 15, 30, 60]  # seconds between restart attempts
BITRATE_ROLLING_WINDOW = 5  # segments per camera for bitrate average
