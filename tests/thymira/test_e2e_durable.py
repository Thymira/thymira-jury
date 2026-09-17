"""Reserved external durability evidence for the PostgreSQL lifecycle repository.

The previous test exercised a raw local worker with a PostgreSQL checkpointer.  That composition
could not prove lifecycle ownership because PostgreSQL lifecycle configuration now fails closed
until the dedicated advisory-lock repository is available.  Local owner crash, fencing and
redelivery evidence lives in ``test_state_lifecycle_integration.py``; this named slow test stays
as the acceptance hook for the supported PostgreSQL worker composition.
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
@pytest.mark.slow
def test_durable_run_resumes_after_worker_crash_exactly_once() -> None:
    """Run the full broker/PostgreSQL lifecycle evidence once the supported backend is composed."""
    pytest.skip(
        "pending supported PostgreSQL lifecycle repository and strict broker worker composition"
    )
