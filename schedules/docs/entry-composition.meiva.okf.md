---
type: doc
title: Schedule entry composition
description: Current ownership boundaries and their executable evidence.
---

# Schedule entry composition

Runner-hosted callbacks borrow their explicit ScheduleInputs without rebinding the shared SDK. A fresh standalone process owns its loaded image, ConfigBoot, one lazy database admission gate and client set. It binds an SDK context with that boot's clock factory and restores the previous context on exit. A primary callback failure survives a secondary client-close failure; normal close failure remains visible.
