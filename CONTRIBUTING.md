# Contributing

## Before opening an issue

Run `solixauto doctor` and include its output. It reports platform, Python
version, dependencies, whether credentials resolve, detected subnets, and
notification status.

**Do not paste secrets.** Never include:

- `notifications.yaml` or any part of it
- your `.env` file, Anker email, or password
- your ntfy topic (anyone with it can read your alerts)

Device profiles contain serial numbers. Redact them if that matters to you.

## Reporting a device that does not work

Include:

- model and part number, e.g. `A1782`
- what `solixauto discover-anker` printed
- whether the device shows as online in the Anker app at the time
- for Shelly devices, generation and model, plus `solixauto conflicts <plug>`

Devices that pair over Bluetooth only cannot be supported here. They publish
nothing to Anker's cloud MQTT broker. That is a hardware limitation, not a bug.

## Field mappings

Telemetry field names come from the upstream
[anker-solix-api](https://github.com/thomluther/anker-solix-api) project.
Corrections to field meanings belong there, not here.

If a field is wrong or missing for your model, that is the right place to
report it. This project only adds derived fields computed from what upstream
already decodes.

## Code changes

- No comments in code; the project is written without them
- Match the existing style rather than introducing a formatter
- Anything touching the engine needs a matching check in `--test` output. If
  a behaviour cannot be observed with `--test`, it is very hard for a user to
  trust it.
- Safety-relevant changes (the battery floor, stale handling, rate limits)
  should come with a description of the failure mode they address

## Testing

There is no unit test suite yet. Contributions that add one are welcome.

At minimum, exercise the paths you touched:

```bash
solixauto run <profile> --test --offline
solixauto run <profile> --test --simulate battery_soc=12
solixauto run <profile> --dry-run --cycles 6
```

A fake Shelly is easy to stand up with aiohttp if you need to test the control
path without hardware; the RPC surface used is small: `/shelly`,
`/rpc/Shelly.GetStatus`, `/rpc/Shelly.GetConfig`, `/rpc/Switch.Set`,
`/rpc/Switch.SetConfig`, `/rpc/Schedule.List`, `/rpc/Schedule.Delete`,
`/rpc/Webhook.List`.
