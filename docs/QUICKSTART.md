# Quickstart: a new product on fakes

Follow these steps to see the whole loop work for your product. Only fake adapters are used, so no email is sent and no money moves.
Every command here is run by `tests/test_cli_quickstart.py`. If that test passes, these steps work.

Commands are written as `sayso ...`. Without installing, use
`python3 -m sayso.cli ...` from the repo root instead.
To install the command: `pip install -e .`

## 1. Make a settings file

```bash
cp examples/acme-dry-run.toml myproduct.toml
```

Edit `[product]`:
- `name`: the display name.
- `slug`: short id, lower-case, used in unit and file names.
- `support_address`: the address customers write to.
- `own_addresses`: every other address of yours. Mail from these is never a customer.

Edit `[operators]`: the board user ids of the people who may approve, with their names.
On fakes any id works. Keep `"1001"` to follow along.

Check it:

```bash
sayso validate --config myproduct.toml
```

A typo, an unknown key, a secret written into the file, or a real adapter while
`mode = "dry-run"` all stop here with `config error: ...` and exit code 1.

## 2. A customer writes in

```bash
sayso fake write-in --config myproduct.toml --from pat@customer.test \
     --subject "Cannot export" --body "Export does nothing."
sayso tick --config myproduct.toml
sayso cases --config myproduct.toml
```

`tick` runs every job once, in order. The case shows `Awaiting approval`: the card
opened with the customer's own email, a draft was written, and a Yes/No vote opened.

The draft is in `state/<slug>/drafts/<case>/reply-v1.txt`.
Open votes are in `state/<slug>/votes.json` (see `sayso status`).

## 3. Approve the reply

```bash
sayso status --config myproduct.toml            # copy the key under live_votes
sayso fake vote --config myproduct.toml --key 'reply:...:v1' --user 1001 --answer yes
sayso tick --config myproduct.toml
sayso cases --config myproduct.toml             # Done, Waiting on customer
```

The sender sent the reply once and read it back from the helpdesk and the
mailbox Sent folder. Try these to see the guards work:
- Vote with `--user 9999`, who is not an operator. Nothing is sent.
- Vote `no`. Nothing is sent.
- Edit the draft file after voting. The vote becomes void and nothing is sent.

## 4. Close

After `quiet_close_days` with no reply from the customer, `tick` opens a close
vote. Approve it the same way and the card ends as `Done`. (The test ages the
fake records rather than waiting three days.)

## 5. Pause

```bash
sayso pause --config myproduct.toml --reason "checking something"
sayso resume --config myproduct.toml
```

While paused, jobs that act (intake, draft, sender, money, dev, release, close)
do nothing. The reconciler and health jobs still report.

## 6. Other paths

- Hand a case to dev: `sayso handoff-dev <case> --config ...`
- Request a refund or cancellation: write the exact spec to a file and run
  `sayso money-request <case> --config ... --spec refund.json --subject "Pat - charged twice"`.
  The spec format is in docs/SETTINGS.md.
- Watch everything at once: `sayso demo`.
- See it in a browser: `sayso dashboard --config myproduct.toml` (read-only; see [DASHBOARD.md](DASHBOARD.md)).

## Next

Connect real systems: [ONBOARDING.md](ONBOARDING.md).
