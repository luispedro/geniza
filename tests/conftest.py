from pathlib import Path

import pytest

CORPUS = Path(__file__).parent / "corpus"


def corpus_files() -> list[Path]:
    return sorted(CORPUS.glob("*.eml"))


@pytest.fixture(params=corpus_files(), ids=lambda p: p.stem)
def corpus_message(request: pytest.FixtureRequest) -> tuple[str, bytes]:
    path: Path = request.param
    return path.stem, path.read_bytes()
