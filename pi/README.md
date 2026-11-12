# pi extension

The Mattermost agent bus as a pi extension (pi-coding-agent 1.3,
`@earendil-works/pi-coding-agent`). `mattermost-agent-bus.ts` is the file pi loads.
Install: [adapters/pi.md](../adapters/pi.md); behaviour:
[arc42 §6.27](../docs/arc42/06-runtime-view.md#627-pi-turn-cycle-through-its-extension).

Tests (a fake hook API and a fake pi `ExtensionAPI`; Node 22.18+):

```bash
node --test pi/test/extension.test.ts
```
