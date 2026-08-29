from pathlib import Path

import pytest

from services.pitr.space_budget import (
    CandidateSpaceBudget,
    InsufficientCandidateSpaceError,
    require_candidate_space,
)


def test_space_preflight_refuses_before_candidate_birth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Usage:
        free = 99

    def disk_usage(_path: str | Path) -> Usage:
        return Usage()

    monkeypatch.setattr("services.pitr.space_budget.shutil.disk_usage", disk_usage)
    budget = CandidateSpaceBudget(25, 25, 25, 25)
    with pytest.raises(InsufficientCandidateSpaceError):
        require_candidate_space(tmp_path, budget)
    assert list(tmp_path.iterdir()) == []
