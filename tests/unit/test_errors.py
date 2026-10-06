import pytest

from reprobe.errors import (
    BudgetExceeded,
    HarnessError,
    ReprobeError,
    ScenarioError,
    SeedError,
)

SUBCLASSES = [ScenarioError, HarnessError, BudgetExceeded, SeedError]


@pytest.mark.parametrize("error", SUBCLASSES)
def test_every_error_derives_from_the_base(error):
    """Callers catch ReprobeError to mean "a failure we intended". Anything that
    escapes that net is a bug, so the hierarchy is asserted rather than assumed."""
    assert issubclass(error, ReprobeError)


def test_base_derives_from_exception_not_baseexception():
    assert issubclass(ReprobeError, Exception)
    assert not issubclass(ReprobeError, KeyboardInterrupt)


@pytest.mark.parametrize("error", SUBCLASSES)
def test_catching_the_base_catches_each_subclass(error):
    with pytest.raises(ReprobeError):
        raise error("boom")


@pytest.mark.parametrize("error", [ReprobeError, *SUBCLASSES])
def test_message_is_preserved(error):
    with pytest.raises(error, match="the specific reason"):
        raise error("the specific reason")


def test_subclasses_are_distinct_so_callers_can_discriminate():
    """A harness failure and a budget stop are handled differently by the loop."""
    assert not issubclass(HarnessError, BudgetExceeded)
    assert not issubclass(BudgetExceeded, HarnessError)
    assert not issubclass(ScenarioError, HarnessError)
    assert not issubclass(SeedError, ScenarioError)
