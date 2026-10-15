from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from base.config import ConfigBoot

before = dict(os.environ)
owners: list[Any] = [ConfigBoot(), ConfigBoot()]
for owner in owners:
    owner.read_process_environment()


def build(owner: Any) -> Any:
    return owner.ensure_eager()


with ThreadPoolExecutor(max_workers=2) as pool:
    models: list[Any] = list(pool.map(build, owners))
assert dict(os.environ) == before, sorted(
    key for key in set(before) | set(os.environ) if before.get(key) != os.environ.get(key)
)
assert all(model.lm.llm_model == "delivered-model" for model in models)
assert all(model.general.timezone == "Asia/Tokyo" for model in models)
assert all(not model.general.message_timestamps for model in models)
assert all(owner.field_explicitly_set("timezone") for owner in owners)
assert all(
    model.lm.gemini_api_key.get_secret_value() == "delivered-provider-key" for model in models
)
complete = owners[0].complete_read_model()
assert complete.lm.llm_model == "delivered-model"
assert dict(os.environ) == before
