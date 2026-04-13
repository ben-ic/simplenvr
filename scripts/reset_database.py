#!/usr/bin/env python3
"""
SimpleNVR Database and Recordings Reset Script

This script completely resets the SimpleNVR database and deletes all recordings.
Use with caution - this operation cannot be undone.

Usage:
    python scripts/reset_database.py
    # or
    ./scripts/reset_database.py
"""

import asyncio
import shutil
import sys
from pathlib import Path

# Add backend to path
backend_dir = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(backend_dir))

from backend import db
from backend.config import DATA_DIR, RECORDINGS_DIR, MOTION_THUMBNAILS_DIR, DB_PATH


async def reset_database():
    """Reset the database and clear all recordings."""
    print("SimpleNVR Database Reset")
    print("=" * 50)
    print()

    # Confirm with user
    print("WARNING: This will permanently delete:")
    print("- All camera configurations")
    print("- All recordings and video files")
    print("- All motion events and thumbnails")
    print("- All tracked events")
    print()
    print(f"Database: {DB_PATH}")
    print(f"Recordings: {RECORDINGS_DIR}")
    print(f"Motion thumbnails: {MOTION_THUMBNAILS_DIR}")
    print()

    response = input("Are you sure you want to continue? (type 'yes' to confirm): ")
    if response.lower() != 'yes':
        print("Reset cancelled.")
        return

    print()
    print("Resetting database...")

    # Connect to database
    conn = await db.init_db()

    try:
        # Clear all tables
        print("- Clearing cameras table...")
        await conn.execute("DELETE FROM cameras")

        print("- Clearing recordings table...")
        await conn.execute("DELETE FROM recordings")

        print("- Clearing motion_events table...")
        await conn.execute("DELETE FROM motion_events")

        print("- Clearing tracked_events table...")
        await conn.execute("DELETE FROM tracked_events")

        # Reset onboarding state
        print("- Resetting onboarding state...")
        await db.set_setting(conn, "onboarding_completed", "false")

        await conn.commit()

        # Clear recordings directory
        print(f"- Clearing recordings directory: {RECORDINGS_DIR}")
        if RECORDINGS_DIR.exists():
            try:
                shutil.rmtree(RECORDINGS_DIR)
                RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                print(f"Warning: Failed to clear recordings directory: {e}")

        # Clear motion thumbnails directory
        print(f"- Clearing motion thumbnails directory: {MOTION_THUMBNAILS_DIR}")
        if MOTION_THUMBNAILS_DIR.exists():
            try:
                shutil.rmtree(MOTION_THUMBNAILS_DIR)
                MOTION_THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                print(f"Warning: Failed to clear motion thumbnails directory: {e}")

        print()
        print("✅ Database and recordings reset successfully!")
        print()
        print("Next steps:")
        print("1. Restart SimpleNVR")
        print("2. Go through the setup process again")
        print("3. Re-discover and configure your cameras")

    except Exception as e:
        print(f"❌ Error during reset: {e}")
        await conn.rollback()
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(reset_database())