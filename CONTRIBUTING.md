# Contributing

Thanks for helping. This loop acts on real customers' email and money, so the
bar is **proof, not plausibility**.

## Setup

```bash
git clone git@github.com:harrythentrepreneur/sayso.git
cd sayso
python3 -m unittest discover -s tests -t .   # must pass before you start
python3 -m sayso.cli demo            # must print DEMO PASS
```

Python 3.11 or newer. The project has no third-party dependencies; please keep
it that way unless a maintainer agrees to add one.

## Workflow

1. Branch from `main`: `feat/...`, `fix/...`, `docs/...`, `test/...`.
2. **One concern per commit.** Use a conventional prefix (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:`).
3. Every change lands through a **pull request**. Never push directly to `main`.
4. Every commit carries both trailers:

   ```
   Co-authored-by: Kaviru Hapuarachchi <94331702+kaviru2@users.noreply.github.com>
   Signed-off-by: Harry <harrythentrepreneur@gmail.com>
   ```

   `git commit -s` adds the sign-off line. Add the co-author line yourself.
5. A merge needs approval from a maintainer (Harry or Kaviru).

## Rules for changes

- **Tests run on fakes only.** No network, no real accounts, no real customer
  data, and no live ids from any product.
- **A new guard needs a mutation entry.** Add it to `MUTATIONS` in
  `scripts/mutate.py`, then show `MUTATION VERDICT: PASS`. A guard you can
  delete while every test stays green is not tested.
- **A red test must fail for the reason you seeded.** Read the assertion
  message, not only the exit code.
- **Assert effects, not source text.** Drive the job against fakes and check
  what landed (the card, the outbox, the refund). Never grep the code.
- **Docs change with behaviour.** `tests/test_docs.py` fails if a documented
  command, settings key, stage or file does not exist.
- **Adapters** follow [docs/ADAPTERS.md](docs/ADAPTERS.md): honour idempotency
  keys, prove effects by read-back, and raise on anything unexpected.

## Stable API (what hosts build on)

Sayso Cloud, the hosted product, installs this package at a pinned commit and
builds on the names listed in `sayso/stable_api.json` (99 today: the job order,
`runtime.Context`, `store.locked`, the vote and console calls, the swipe deck
and others). `tests/test_stable_api.py` fails if a change removes one, renames
or reorders a parameter, adds a required one, or changes a constant's value.
Adding things is always fine: a new optional parameter, a new method, a new key.

If you mean to change a listed name:

1. Run `python3 scripts/stable_api.py --write` to re-record it.
2. Say so in the PR ("stable API: `votes.open_vote` gains required `tenant`").
3. Expect the `sayso-cloud/compat` status on your PR head to say whether
   Sayso Cloud still passes its own suite against your change. It is posted
   by the maintainers' checker for branches in this repo (not forks); the
   status carries only a pass/fail line, never Cloud code.

Hosting-only behaviour (sign-in, tenancy, vaults, billing) belongs in the host,
not here. A fix that every self-hoster would want belongs here first; hosts
receive it by moving their pin.

## Pull request checklist

The PR template repeats this list.

- [ ] `python3 -m unittest discover -s tests -t .` passes
- [ ] `python3 scripts/mutate.py` prints `MUTATION VERDICT: PASS`
- [ ] `python3 -m sayso.cli demo` prints `DEMO PASS`
- [ ] `python3 scripts/stable_api.py` prints `STABLE API: OK` (or the PR says which listed name changes)
- [ ] Docs and CHANGELOG are updated
- [ ] No secrets, customer data or live ids

## Releases

Maintainers only:

1. Update `CHANGELOG.md` and the version in `pyproject.toml` and `sayso/__init__.py`.
2. Merge the change.
3. Tag `vX.Y.Z` on `main`.
4. Publish a GitHub release, using that version's changelog section as the notes.
