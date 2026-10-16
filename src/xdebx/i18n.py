from .runtime import PROJECT, read_json


def tr(ctx, key, **values):
    messages = read_json(PROJECT / "src/locales" / (ctx.settings.get("language", "EN") + ".json"))
    return messages.get(key, key).format(**values)
