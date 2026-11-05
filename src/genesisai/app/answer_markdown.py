"""Markdown rendering shared by current answers, tool-turn commentary and history."""

from rich.markdown import Markdown, TableElement
from rich.text import Text

from genesisai.app.terminal_text import terminal_text


class AnswerTable(TableElement):
    def __rich_console__(self, console, options):
        if options.max_width >= 100 or not (self.header and self.header.row and self.body):
            yield from super().__rich_console__(console, options)
            return
        headings = self.header.row.cells
        for row in self.body.rows:
            fields = Text('• ')
            for index, (heading, cell) in enumerate(zip(headings, row.cells)):
                if index:
                    fields.append('；')
                fields.append_text(heading.content)
                fields.append('：')
                fields.append_text(cell.content)
            yield fields


class AnswerMarkdown(Markdown):
    elements = {**Markdown.elements, 'table_open': AnswerTable}

    def __init__(self, answer, *, hyperlinks):
        super().__init__(terminal_text(answer), justify='left', hyperlinks=hyperlinks)
        # In news lists, the title, attribution and summary often use single newlines.
        for block in self.parsed:
            for token in block.children or ():
                if token.type == 'softbreak':
                    token.type = 'hardbreak'
