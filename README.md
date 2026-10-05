# claude-lb

Claude/Anthropic account load balancer and proxy with a usage dashboard.

Pool multiple Claude accounts (claude.ai OAuth seats and Anthropic API keys) behind a
single Anthropic-compatible endpoint (`/v1/messages`), with account-level rate-limit
tracking, failover, and usage reporting.

> Forked from [Soju06/codex-lb](https://github.com/Soju06/codex-lb) (MIT), a Codex/ChatGPT
> account load balancer. claude-lb adapts its load-balancing, accounts, usage, and dashboard
> machinery to the Anthropic ecosystem.

## Status

Work in progress. The adaptation roadmap lives in
`openspec/changes/anthropic-upstream-adaptation/`:

1. ~~Fork scaffold: rebrand, strip Codex-only machinery~~ (landed)
2. ~~Anthropic account model — claude.ai OAuth (PKCE) accounts + Anthropic API key accounts~~
   (landed — live token-endpoint verification pending until a Claude account is available;
   see `openspec/changes/add-anthropic-account-model/`)
3. Proxy core rewrite — `/v1/messages` downstream, account-pool load balancing, 5-hour-window
   rate limiting, SSE streaming, failover invariants *(in progress — working slice landed:
   `/v1/messages` streaming SSE + non-streaming, bounded failover, oauth-seat token refresh,
   Anthropic error envelope; live OAuth verification against a real Claude account pending)*
4. Usage tracking + dashboard adaptation

The Anthropic proxy slice is implemented but not yet live-verified; the bundled Codex proxy
core remains inert reference code, not a working proxy.

## License

MIT, inherited from codex-lb.
