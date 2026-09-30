# Player profiles

One YAML file per additional player. Each profile is a **separate player with
separate data**: its own SQLite database and its own immutable raw PGN
directory. Nothing is shared except the code and the analysis settings
(engine path, depth, thresholds, phase rules) inherited from `config.yaml`.

    python -m chess_coach --profile <name> update
    python -m chess_coach --profile <name> report
    python -m chess_coach profiles            # list everyone

The `default` profile is implicit and is configured by `config.local.yaml`, not
by a file here. It keeps the original paths so existing data is untouched.

## Isolation

A profile that does not declare `paths` automatically gets
`data/profiles/<name>/`. That default is deliberate: inheriting the base
config's paths would silently point two players at one database. Declaring
paths that collide with the default profile is refused at load time, and every
database records the username it belongs to and refuses to open for anyone
else.

## Adding a player

Copy `example.example.yaml` to `<name>.yaml` and fill in the username and the
contact string chess.com asks for. Everything in this directory except the
example and this README is git-ignored, because it holds personal data.
