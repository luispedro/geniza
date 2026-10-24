import subprocess
import sys

from conftest import CORPUS, corpus_files


def test_corpus_is_up_to_date() -> None:
    proc = subprocess.run(
        [sys.executable, str(CORPUS / "generate.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_corpus_is_synthetic() -> None:
    # A cheap guard against real mail slipping in: every address header in
    # the corpus uses a reserved example domain.
    import re

    address_line = re.compile(r"^(?:From|To|Cc|Reply-To|Sender):.*$", re.M | re.I)
    for path in corpus_files():
        text = path.read_bytes().decode("latin-1")
        lines = "\n".join(address_line.findall(text))
        for domain in re.findall(r"@([A-Za-z0-9.-]+\.[A-Za-z]+)", lines):
            assert domain.lower().endswith(
                ("example.org", "example.net", "example.com")
            ), (path.name, domain)
