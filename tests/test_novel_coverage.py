"""Cross-chapter evidence is paged, hash-bound and never a full-book guarantee."""
from pathlib import Path
import pytest
from genesisai.core.extensions.tools.novel.chapter_outline.implementation import Implementation as Outline
from genesisai.core.extensions.tools.novel.consistency_check.implementation import Implementation as Check
from genesisai.core.extensions.tools.novel.chapter_read.implementation import Implementation as Read
from genesisai.core.extensions.tools.novel.chapter_list.implementation import Implementation as ListChapters
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.security import ToolError
from test_upgrade_p2_p6 import runtime_for


def test_all_chapter_blocks_reach_tail_and_cross_chapter_facts_survive_resume(tmp_path):
    workspace, _, store, runtime = runtime_for(tmp_path)
    try:
        first = workspace / 'chapter1.md'
        first.write_text('# Start\n' + '背景。\n' * 3000 + '\nAlice is 20 years old.\n', encoding='utf-8')
        second = workspace / 'chapter2.md'
        second.write_text('# Later\nAlice is 25 years old.\n张三今年20岁。', encoding='utf-8')
        args = {'path': 'chapter1.md', 'limit': 1000}
        pages = []
        while True:
            page = Outline().execute(runtime.context, args)
            pages.append(page)
            if page['next'] is None: break
            args = page['next']
        assert len(pages) > 10
        assert any(f['entity'] == 'Alice' and f['attribute'] == 'age' for f in pages[-1]['facts'])
        assert all(a['coverage']['end'] == b['coverage']['start'] for a, b in zip(pages, pages[1:]))
        reopened = Session(store.root, store.id)
        other = ToolRuntime(reopened, runtime.access, providers=[])
        try:
            result = Check().execute(other.context, {'path': 'chapter2.md', 'fact_limit': 1})
            assert result['next_fact_offset'] == 1
            assert result['assessment'] == 'heuristic_candidates_only'
            assert 'consistent' not in result
            difference = next(item for item in result['potential_differences'] if item['entity'] == 'Alice')
            assert {item['value'] for item in difference['evidence']} == {'20', '25'}
            assert result['chapter_coverage'][str(first)]['complete']
            assert result['chapter_coverage'][str(second)]['complete']
            assert any(f['entity'] == '张三' for f in result['facts'])
            first.write_text('Alice is 25 years old.', encoding='utf-8')
            refreshed = Check().execute(other.context, {'path': 'chapter2.md'})
            assert str(first) not in refreshed['chapter_coverage']
            assert not refreshed['potential_differences']
        finally:
            other.close()
    finally:
        runtime.close()


def test_partial_coverage_and_stale_cursor_are_explicit(tmp_path):
    workspace, _, _, runtime = runtime_for(tmp_path)
    try:
        source = workspace / 'chapter.md'
        source.write_text('Alice says hello.\n' * 3000, encoding='utf-8')
        first = Check().execute(runtime.context, {'path': 'chapter.md', 'limit': 500})
        assert not first['chapter_coverage'][str(source)]['complete']
        assert first['coverage']['end'] == 500 and first['next']['offset'] == 500
        runtime.context_budgeter.configure({'max_input_tokens': 8000})
        page = Read().execute(runtime.context, {'path': 'chapter.md'})
        assert 0 < len(page['text']) < 20000 and page['next_offset'] == len(page['text'])
        source.write_text('changed', encoding='utf-8')
        with pytest.raises(ToolError, match='章节已变化'):
            Check().execute(runtime.context, first['next'])
    finally:
        runtime.close()


def test_chapter_directory_has_no_1000_file_total_limit(tmp_path):
    workspace, _, _, runtime = runtime_for(tmp_path)
    try:
        for index in range(1005):
            (workspace / f'chapter{index:04d}.md').write_text('# Heading\nTail', encoding='utf-8')
        result = ListChapters().execute(runtime.context, {'path': '.'})
        assert result['chapter_count'] == 1005
        assert result['chapters'][-1]['number'] == 1004
        assert result['total_words'] == 1005 * len('# Heading\nTail')
    finally:
        runtime.close()
