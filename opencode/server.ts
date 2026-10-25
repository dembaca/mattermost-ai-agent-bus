// The entry OpenCode 2 loads when a `plugins` entry names this directory
// (`{"package": "file:///…/opencode", "options": {…}}`, adapters/opencode.md):
// it looks for server.ts, then index.ts. The plugin itself: RT-6.20.
export { default } from "./plugin/mattermost-agent-bus.ts"
