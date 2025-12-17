from stashapi import log as stash_log
from stashapi.stashapp import StashInterface
import tomli
from pathlib import Path
toml = Path("dev.toml")
config = {}
with toml.open("rb") as f:
    config.update(tomli.load(f))
print(config)
stash = StashInterface(config.get("STASH_CONFIG"))
print(stash.get_configuration().get("general").get("databasePath"))
