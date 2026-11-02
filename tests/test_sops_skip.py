"""Without a SOPS decryption key the needs_sops_key tests skip, loudly and with the reason."""

import os
import subprocess
import sys

from conftest import ROOT, SOPS_SKIP_REASON


def test_decrypting_tests_skip_with_a_logged_reason_when_no_key_is_available(tmp_path):
    gnupg = tmp_path / "gnupg"
    gnupg.mkdir(mode=0o700)
    # An empty GNUPGHOME is what a CI runner has: sops finds no private key to decrypt with.
    env = {k: v for k, v in os.environ.items() if not k.startswith("SOPS_")}
    env["GNUPGHOME"] = str(gnupg)
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "tests/test_secrets.py",
            "--tb=no",
            "--show-capture=no",
            "-rs",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert SOPS_SKIP_REASON in out, out
    assert "failed" not in out and "skipped" in out, out
