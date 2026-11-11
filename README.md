# venue-booking

Book a court on a web-based venue reservation system the moment reservations open.

The script waits (overnight if you like), renews your login, tries your courts and
times in order of preference, and keeps your Mac awake. By default you solve one
slider captcha just before the opening time.

The booking site it talks to is **not hard-coded** — you point it at your own
site and venues in a local config file (see [Configure the site](#configure-the-site)),
so this repository contains no particular institution's details.

## Requirements

- macOS with Chrome installed
- [uv](https://docs.astral.sh/uv/)
- An account on the booking site (single sign-on)

## Setup

Install dependencies:

```bash
uv sync
```

Create a config file:

```bash
uv run venue-booking init-config      # writes ~/.config/venue-booking/config.toml (chmod 600)
```

### Configure the site

Open `~/.config/venue-booking/config.toml` and fill in the `[site]` and `[venues]`
sections. These come from a capture of your own browser session on the booking
site (DevTools → Network, or a saved `.har`):

```toml
[site]
base_url = 'https://booking.example.edu'   # the host you book on
sso_host = 'id.example.edu'                 # where the login form lives
app_id   = '...'                            # the `appId` query param on API calls
sign_key = '...'                            # the key used to build the `sign` param

[venues.main]
name = 'Main hall'
scene_uuid      = '...'   # copy from a `reserve/current/page` request body
dev_kind_uuid   = '...'
class_type_uuid = '...'
```

See [`config.example.toml`](config.example.toml) for the full, commented template.

### Log in

```bash
uv run venue-booking auth
```

Complete sign-on in the Chrome window, including any captcha or 2FA. Later logins
are automatic unless the site needs your input again.

To let the script re-enter your credentials after the SSO session expires, add
them to the config file:

```toml
[sso]
username = 'xxxxxxxxx'
password = 'your password'
```

The password is stored as plain text; keep the file private (`chmod 600`).
Alternatively, use the macOS Keychain:

```bash
uv run venue-booking auth --remember-password
```

Use `uv run venue-booking whoami` to check your login and credential source.

## Usage

Check availability:

```bash
uv run venue-booking list
uv run venue-booking list --date today
```

Practise the captcha without booking:

```bash
uv run venue-booking try-captcha
```

Start a booking that waits for the opening time:

```bash
# Default preferences: 12:00 on court 4, then court 5
uv run venue-booking book

# Custom preferences
uv run venue-booking book --time 12:00 --time 14:00 --court 6 --court 7
```

Book an available slot immediately:

```bash
uv run venue-booking book --now --date today --time 21:00
```

Repeat `--time` and `--court` in order of preference. Times have priority over
courts; `--any-court` accepts every court. A time selects the session containing
it, while an exact range such as `--time 11:30-13:00` selects that session only.

You can save defaults in the `[booking]` section of the config file, including
separate `[booking.weekday]` / `[booking.weekend]` choices. Command-line
preferences override saved defaults.

The opening time and how many days ahead reservations open are site-specific;
set them with `--at HH:MM` (default `08:00`) and `--date +N` (default `+3`).
Run `uv run venue-booking book --help` for scheduling, payment, alarm, and other options.

## Overnight checklist

- Keep the Mac **plugged in, lid open, and volume up**.
- Leave the terminal open and allow notifications.
- Login is refreshed before opening (around `--login-at`, default 07:30), and the
  captcha alarm sounds shortly before opening (default 120 s before).
- Use `--dry-run` to rehearse without sending a booking request.

The script keeps the Mac from idle-sleeping, but closing the lid can still put it to sleep.

## Automatic captcha solving

```bash
uv run venue-booking book --auto-captcha
```

The solver submits the slider only when it is confident; otherwise it falls back
to the alarm and manual solving. Rehearse first:

```bash
uv run venue-booking try-captcha --auto-captcha
```

## Payment

- `--pay online` is the default. Pay on the website **within 60 minutes**, or the booking is cancelled.
- `--pay offline` requires payment at the cashier before the session. Missed offline payments can affect your booking rights.

## Development

```bash
uv run pytest
```
