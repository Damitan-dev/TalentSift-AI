import os
from pathlib import Path


# ---------------------------------------------------------
# TALENTSIFT RUNTIME DATA DIRECTORY
# ---------------------------------------------------------
#
# Local development:
#
#     TALENTSIFT_DATA_DIR is not set
#            ↓
#     use ./data
#
# Deployment:
#
#     hosting platform can set:
#
#     TALENTSIFT_DATA_DIR=/some/persistent/disk/path
#
# This allows SQLite, Scorecards, and fairness-test data
# to survive application restarts when persistent storage
# is mounted by the deployment platform.

DATA_DIR = Path(
    os.environ.get(
        "TALENTSIFT_DATA_DIR",
        "data",
    )
)


DATA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)