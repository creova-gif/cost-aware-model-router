from cost_aware_model_router import healthcheck


def test_healthcheck() -> None:
    assert healthcheck() == "ok"
