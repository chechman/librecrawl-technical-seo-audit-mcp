# Security hardening

This fork hardens the MCP server against hostile **audited websites** — the
realistic threat when you point the crawler at a site you don't control.

## Fixes applied in this branch

| # | Issue | Fix |
|---|-------|-----|
| 1 | Crawled HTML → WeasyPrint `file://` local-file read / `http://` SSRF during PDF render | `pdf_report._blocking_url_fetcher` refuses all resource loads; `server._md()` escapes crawled text in the report |
| 2 | SSRF guard bypass (no DNS resolution, blind redirect-follow) in schema fetches | `ssrf_guard.validate_url` resolves DNS and validates every IP + each redirect hop; weak `_validate_public_url` removed |
| 3 | Unguarded SSRF in site-check / robots-declared sitemaps / sitemap-fill / external-link validation, auto-triggered by a hostile site | all attacker-influenced fetches routed through `ssrf_guard` (sync helper + async event-hook) |
| 4 | `str.startswith` containment let sibling-prefixed paths escape `REPORTS_DIR` | `server._within_reports` uses `Path.is_relative_to` |

The SSRF policy is deny-by-default: any host resolving to a
private / loopback / link-local (incl. `169.254.169.254`) / reserved / multicast
address is refused. Tests: `python3 -m unittest discover -s tests`.

## ⚠️ Required operational control: network-isolate the deployment

The fixes above cover **this repo's** fetches. The bulk page crawling is done by
the upstream **LibreCrawl** container, which this code cannot patch — a malicious
target whose pages redirect to internal addresses could still SSRF *through the
upstream crawler*.

So when auditing untrusted sites — **especially on a cloud VPS** — also restrict
egress at the network layer. Example: run on a Docker network with firewall rules
that drop traffic to RFC1918, loopback, and the cloud metadata IP:

```bash
# Block the cloud metadata endpoint for the crawler's network namespace
iptables -I DOCKER-USER -d 169.254.169.254 -j DROP
iptables -I DOCKER-USER -d 10.0.0.0/8      -j DROP
iptables -I DOCKER-USER -d 172.16.0.0/12   -j DROP
iptables -I DOCKER-USER -d 192.168.0.0/16  -j DROP
```

This single control also covers the upstream crawler and is considered
non-negotiable for VPS deployments that audit arbitrary URLs.

## Other notes carried over from review

- The installer uses `curl | bash` and re-downloads modules from `main` at
  install time — pin to a reviewed commit / your fork to avoid TOCTOU drift.
- The bundled LibreCrawl runs with `DANGEROUSLY_SKIP_AUTH=true` (localhost-bound).
  Acceptable single-user; isolate on shared hosts.
