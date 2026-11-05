from genesisai.shared.security import ToolError


class Implementation:
    def prepare(self, context, args):
        question = args["question"].strip()
        options = [option.strip() for option in args.get("options", [])]
        if not question or any(not option for option in options) or len(set(options)) != len(options):
            raise ToolError("validation_error", "问题和选项须为非空文本，选项须互不重复")
        self.question = {"question": question, "options": options}
        return self.question

    def execute(self, context, args):
        return self.question
