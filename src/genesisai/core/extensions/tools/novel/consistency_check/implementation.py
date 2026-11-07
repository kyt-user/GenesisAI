"""检查已覆盖章节的显式事实差异，不把未读部分当作一致。"""
from genesisai.core.extensions.tools.novel.facts import extract_block, fact_table
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args):
        result = extract_block(context, args)
        facts, coverage = fact_table(context, args.get('group', '.'))
        buckets = {}
        for fact in facts:
            if fact['attribute'] != 'mention':
                buckets.setdefault((fact['entity'], fact['attribute']), []).append(fact)
        differences = [{'entity': entity, 'attribute': attribute, 'evidence': evidence,
                        'assessment': 'requires_human_review_or_timeline_context'}
                       for (entity, attribute), evidence in buckets.items() if len({f['value'] for f in evidence}) > 1]
        offset, limit = args.get('fact_offset', 0), args.get('fact_limit', 100)
        result.update(fact_table=facts[offset:offset+limit], total_facts=len(facts),
                      next_fact_offset=offset+limit if offset+limit < len(facts) else None,
                      chapter_coverage=coverage, potential_differences=differences,
                      scope_notice='仅检查此会话已抽取且哈希仍有效的章节块；未扫描章节未被审查，启发式不是语义一致性证明')
        return result
