"""KickDropsMiner - Main Entry Point"""
import sys

from ui.app import App
from utils.helpers import LOCK_FILE, lock_file


if __name__ == "__main__":
    lock_success, lock_handle = lock_file(LOCK_FILE)
    if not lock_success:
        print("Kick Drop Miner is already running.")
        try:
            lock_handle.close()
        finally:
            sys.exit(1)

    app = App()
    try:
        app.mainloop()
    finally:
        lock_handle.close()
