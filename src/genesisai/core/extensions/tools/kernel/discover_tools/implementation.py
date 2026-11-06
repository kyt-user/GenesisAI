class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        return context.runtime.discover_tools(args["query"], args.get("limit", 6), args.get("activate", True))
