"""Consumer-local opt-in fixtures for this test directory."""

from services.entrypoints.im_bridge.tests.adapters.weixin_native import (
    native_weixin as native_weixin,
)
from tests.fixtures.log_capture import loguru_records as loguru_records
