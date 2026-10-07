from src.cache import Cache


def test_round_trips_a_value():
    cache = Cache()
    cache.put("a", 1)
    assert cache.get("a") == 1
