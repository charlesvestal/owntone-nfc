"""Guards on the provisioning path.

There is no harness that can run provision.sh here - it wants root and a Pi -
so these check the shape of the thing rather than its behaviour. That is worth
doing for one specific property: the SMB read size.

A rebuild that comes back with SMB3's 4MB default does not look broken. The
library mounts, albums play, and the only symptom is a HomePod stereo pair
drifting in and out of sync, because 4MB reads monopolise the air and AirPlay 2
timing packets queue behind them. It presents as a speaker fault or a Wi-Fi
fault, and an afternoon went into the radio before anyone looked at rsize.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROVISION = ROOT / "deploy" / "provision.sh"
RUNBOOK = ROOT / "docs" / "runbook.md"

READ_SIZE = "rsize=131072"


def test_provision_caps_the_smb_read_size():
    """Checks the options actually written, not merely a mention of them.

    The first version of this test matched anywhere in the file and so passed
    while the real MOUNT_OPTS said rsize=4194304 - the help text further down
    mentions the correct value and that was enough to satisfy it.
    """
    opts = [ln for ln in PROVISION.read_text().splitlines()
            if ln.strip().startswith("MOUNT_OPTS=")]
    assert opts, "provision.sh no longer builds MOUNT_OPTS"
    assert any(READ_SIZE in ln for ln in opts), (
        "the mount options provision.sh writes must pin the SMB read size; "
        "without it a rebuilt box returns to the 4MB default and the stereo "
        f"pair loses sync. Found: {opts}"
    )


def test_provision_does_not_hardcode_the_home_network():
    """The share is passed in. Keeping the home network out of a public repo
    was a deliberate commit, and writing the mount must not undo it."""
    text = PROVISION.read_text()
    assert "NAS_SHARE" in text
    # An RFC1918 literal followed by a share path is the shape to catch.
    import re
    leaked = re.findall(r"//(?:192\.168|10\.|172\.(?:1[6-9]|2\d|3[01]))[\d.]*/\S+", text)
    # The usage comment shows an example; anything else is a real address.
    assert all("10.0.0.5" in m for m in leaked), f"looks like a real share: {leaked}"


def test_the_runbook_and_provision_agree_on_the_read_size():
    """Docs drifting from the script is how the 5GHz story stayed wrong for
    months - the runbook was the only record and nothing checked it."""
    assert READ_SIZE in RUNBOOK.read_text(), (
        "docs/runbook.md documents the fstab line; it must carry the same "
        "read size provision.sh writes"
    )
