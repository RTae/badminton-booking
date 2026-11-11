# venue-booking

Book a court on an online booking site as soon as reservations open. The script waits until bookings open, including overnight, refreshes your login,
and tries your preferred courts and times in order. So you need your computer awake while it waits. You solve one slider captcha just before the opening.

You choose which site and venues to book in a local config file, so the code
itself names no specific site. See [Configure the site](#configure-the-site).

## Requirements

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
sections. Get the values from a recording of your own browser session on the
booking site, using your browser's developer tools (Network tab) or a saved
`.har` file:

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

See [`config.example.toml`](config.example.toml) for the complete template, with comments.

### Log in

```bash
uv run venue-booking auth
```

Complete sign-in in the Chrome window, including any captcha or 2FA. Later logins are automatic unless the site needs your input again.

To let the script re-enter your credentials after the SSO session expires, add them to the config file:

```toml
[sso]
username = 'xxxxxxxxx'
password = 'your password'
```

The password is stored as plain text, so keep the file private (`chmod 600`).
Or store it in the macOS Keychain:

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
courts, and `--any-court` accepts every court. A time selects the session that
contains it. An exact range such as `--time 11:30-13:00` selects that session only.

You can save defaults in the `[booking]` section of the config file, with
separate `[booking.weekday]` and `[booking.weekend]` choices if you want them. Command-line preferences override saved defaults.

The opening time and how many days ahead reservations open depend on the site. Set them with `--at HH:MM` (default `08:00`) and `--date +N` (default `+3`). Run `uv run venue-booking book --help` for the scheduling, payment, and alarm options.

## Overnight checklist

- Keep the notebook plugged in, with the lid open and the volume up.
- Leave the terminal open and allow notifications.
- The script refreshes your login before bookings open (at `--login-at`, default
  07:30) and sounds the captcha alarm shortly before (default 120 seconds before).
- Use `--dry-run` to rehearse without sending a booking request.

The script keeps the Mac from idle-sleeping, but closing the lid can still put it to sleep.

## Automatic captcha solving

```bash
uv run venue-booking book --auto-captcha
```

The solver moves the slider only when it is confident. Otherwise it rings the
alarm so you solve it yourself. Rehearse first:

```bash
uv run venue-booking try-captcha --auto-captcha
```

## Payment

- `--pay online` is the default. Pay on the website within 60 minutes, or the booking is cancelled.
- `--pay offline` lets you pay at the cashier before the session. Missing an offline payment can affect your booking rights.

## Development

```bash
uv run pytest
```
