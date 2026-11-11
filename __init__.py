def register(ctx):
    from .adapter import register as register_adapter
    register_adapter(ctx)
