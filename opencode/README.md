# OpenCode plugin

The Mattermost agent bus as an OpenCode plugin (plugin API V1, OpenCode 1.x).
Install and options: [adapters/opencode.md](../adapters/opencode.md); behaviour:
[arc42 §6.20](../docs/arc42/06-runtime-view.md#620-opencode-turn-cycle-through-its-plugin).

Tests (a fake hook API and a fake OpenCode client; Node 22.18+ or Bun):

```bash
node --test opencode/test/plugin.test.ts
```
