# hubra-cli

The `hubra` command for the hubra-app GitHub repos and the running Hubra:
status of every repo, pull requests, merges, plugin releases, signing
approvals, and Hubra's health, alerts, errors and jobs. `hubra tui` shows all
of it on one full-screen view.

![Pull requests in the TUI](docs/screenshots/4-prs.png)

## Install

Needs Python 3.9+ and the [gh CLI](https://cli.github.com) logged in as an
owner of hubra-app (`gh auth login`; if asked: `gh auth refresh -s
admin:org,workflow`).

```sh
pipx install "git+https://github.com/hubra-app/hubra-cli#egg=hubra-cli[tui]"
pipx upgrade hubra-cli                 # later, for a new version
```

Without the `[tui]` extra everything except `hubra tui` works, using only
the standard library.

## Commands

```sh
hubra status                  # every repo in one table (one GraphQL query)
hubra prs                     # every open PR with checks, review, link
hubra pr budgets#12 --diff    # one PR in depth (--web opens it)
hubra merge                   # auto-merge ready plugin PRs
hubra merge budgets#12        # only this PR (--now merges at once, --ready un-drafts)
hubra release                 # release new plugin.yaml versions, with run links
hubra ship                    # merge, then release
hubra approve                 # approve signing runs waiting for you
hubra runs --failed           # recent failed Actions runs
hubra open budgets#12         # open a repo / PR / --actions / --releases page
hubra setup                   # create new plugin repos, configure all
hubra key signing.key         # store the plugin signing key
hubra login                   # save a Hubra API token
hubra hubra health            # also: alerts, errors, jobs, whoami, get PATH
hubra tui                     # everything on one screen
```

Repo names are plugin short names (`budgets`) or `hubra`, `hubra.club`,
`hubra-plugins`; `name#12` or a PR URL names one pull request. `hubra --help`
and `hubra <command> --help` explain every option. `--dry-run` shows what
would change without changing anything.

After a release, the plugin lists are still yours (hubra
docs/plugin-releases.md: hubra README, hubra-plugins README, hubra.club
Modules page).

## Hubra API

`hubra hubra …` and the TUI's Hubra tab read the running Hubra with an API
token (manage › API-Tokens; a read-only platform token with
`platform.diagnostics` is enough). `hubra login` keeps it in
`~/.config/hubra/token`, readable only by you; `HUBRA_TOKEN` overrides it.
`HUBRA_API_URL` (default `https://api.hubra.club`) and `HUBRA_MANAGE_URL`
point at another install.

## TUI keys

`r` refresh · `o` open in browser · `m` auto-merge · `M` merge now · `y` mark
ready · `d` diff · `R` release · `a` approve signing · `q` quit. Every change
asks first; `hubra tui --dry-run` only shows what it would do.

## Development

```sh
pip install -e ".[dev]"
ruff check . && pytest
```

The tests never touch GitHub or Hubra: `tests/fake_gh` stands in for gh with
a made-up org and `tests/fake_hubra.py` for the Hubra API. To try a command
by hand against them:

```sh
mkdir -p /tmp/fakebin && ln -sf "$PWD/tests/fake_gh" /tmp/fakebin/gh
python tests/fake_hubra.py 8765 &
export PATH=/tmp/fakebin:$PATH HUBRA_API_URL=http://127.0.0.1:8765 \
       HUBRA_TOKEN=hbr_plat_AAAAAAAAAAAA_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
hubra status && hubra ship --dry-run
```

## Licence

Source-available, see [LICENSE](LICENSE).
