# Installing the timers (Linux, systemd user units)

The loop never installs, starts or enables anything itself. You do it by hand,
after you review the files.

```bash
# 1. Code and a dedicated virtualenv (no third-party dependencies)
git clone git@github.com:harrythentrepreneur/sayso.git /opt/sayso
python3 -m venv /opt/sayso/.venv && /opt/sayso/.venv/bin/pip install -e /opt/sayso

# 2. Secrets file (see ONBOARDING.md)
install -m 600 /dev/null ~/.config/sayso/acme.env && $EDITOR ~/.config/sayso/acme.env

# 3. Render the units, then READ them
/opt/sayso/.venv/bin/sayso timers render --config /etc/sayso/acme.toml --out /tmp/sayso-units \
    --python /opt/sayso/.venv/bin/python
less /tmp/sayso-units/*

# 4. Install and start (this is your decision, not the loop's)
cp /tmp/sayso-units/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sayso-acme-health.timer sayso-acme-reconcile.timer   # read-only first
# watch for a day, then:
systemctl --user enable --now $(cd ~/.config/systemd/user && ls sayso-acme-*.timer)
loginctl enable-linger "$USER"     # keep timers running after logout
```

Each job gets a `.service` (oneshot, it loads the secrets file) and a `.timer`
(`OnUnitInactiveSec` = the interval from `[jobs]`, `Persistent=true`). Twenty
files per product.

Check:
```bash
systemctl --user list-timers 'sayso-acme-*'
journalctl --user -u sayso-acme-sender.service -n 50
sayso status --config /etc/sayso/acme.toml
```

Several products on one host: one settings file, one state directory and one
set of units for each product. The units are named by slug, so they never collide.

Uninstall: `systemctl --user disable --now 'sayso-acme-*.timer'` and remove the
unit files. The state directory is kept. Archive it; it holds the approval records.
