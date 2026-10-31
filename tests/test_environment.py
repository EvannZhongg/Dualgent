import pytest
from conftest import MemoryDriver

from dualgent.contracts import InteractionError
from dualgent.registry import Environments


async def test_user_continuation_can_refresh_same_origin_metadata():
    driver = MemoryDriver()
    bound = driver.surface.model_copy(deep=True)
    driver.surface.url = "http://localhost/receipt"
    refreshed = await Environments({"web": driver}).refresh_bindings({bound.id: bound})
    assert refreshed[bound.id].url == "http://localhost/receipt"


async def test_user_continuation_cannot_expand_origin_or_replace_surface():
    driver = MemoryDriver()
    bound = driver.surface.model_copy(deep=True)
    driver.surface.url = "https://other.test/"
    with pytest.raises(InteractionError):
        await Environments({"web": driver}).refresh_bindings({bound.id: bound})
    driver.surface.id = "web:new-session:1"
    with pytest.raises(InteractionError):
        await Environments({"web": driver}).refresh_bindings({bound.id: bound})
