"""
One-time (or re-auth) Garmin login helper.

This script does ONLY authentication. It reads a temporary credentials file,
logs into Garmin Connect (handling an MFA/2FA code if Garmin asks for one),
and saves a reusable login token to .tokens/ (the library does this
automatically once it's pointed at that folder). Your password is never
written to disk by this script -- only the resulting session token is kept.

How the MFA handshake works here: if Garmin asks for a 2FA code, this script
writes "MFA_REQUIRED" to .tokens/.login_status.txt and then waits (polling)
for a file called .tokens/.mfa_code.txt to appear. Something else (the
assistant driving this) is responsible for writing that file once the user
supplies the code.
"""

import json
import sys
import time
from pathlib import Path

import garminconnect

BASE = Path(__file__).resolve().parent
TOKENSTORE = BASE / ".tokens"
CRED_FILE = TOKENSTORE / ".cred_tmp.json"
MFA_FILE = TOKENSTORE / ".mfa_code.txt"
STATUS_FILE = TOKENSTORE / ".login_status.txt"


def write_status(msg: str) -> None:
    STATUS_FILE.write_text(msg)
    print(msg, flush=True)


def prompt_mfa() -> str:
    """Called by the library mid-login if Garmin wants a 2FA code."""
    write_status("MFA_REQUIRED")
    for _ in range(300):  # wait up to ~5 minutes for the code
        if MFA_FILE.exists():
            code = MFA_FILE.read_text().strip()
            MFA_FILE.unlink()
            return code
        time.sleep(1)
    raise TimeoutError("Timed out waiting for the 2FA code")


def main() -> None:
    TOKENSTORE.mkdir(exist_ok=True)

    if not CRED_FILE.exists():
        write_status("ERROR: no credentials file found")
        sys.exit(1)

    try:
        creds = json.loads(CRED_FILE.read_text())
        email = creds["email"]
        password = creds["password"]
    except Exception as e:
        write_status(f"ERROR: could not read credentials ({e})")
        sys.exit(1)

    try:
        garmin = garminconnect.Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
        write_status("LOGGING_IN")
        garmin.login(str(TOKENSTORE))
        write_status("SUCCESS")

    except Exception as e:
        write_status(f"ERROR: login failed ({e})")
        sys.exit(1)

    finally:
        # Always scrub the temporary credentials file, success or failure.
        try:
            CRED_FILE.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
