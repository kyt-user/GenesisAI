import pytest

from genesisai.agent.loop import Runner
from genesisai.agent.recovery import RecoveryMixin
from genesisai.shared.messages import Response
from test_acceptance import FakeModel
from test_upgrade_p2_p6 import runtime_for


@pytest.mark.parametrize('text', [
    '1ea072356b5a2323e960e83c5c21c3a516ca91cf5cce2742226f89cadbc064ee',
    'MARKER_123cad456789abcd', 'src_abcd123cad456789', '段落3100–3105，offset=3100，limit=10',
    'file_USD123.json', 'prefixCAD12suffix', 'Code123USDValue', 'USD9.99identifier', 'CAD1,200key',
])
def test_identifiers_and_paragraph_offsets_are_not_prices(text):
    assert RecoveryMixin._price_claims(text) == set()


@pytest.mark.parametrize('text,expected', [
    ('USD9', {('USD', '9')}), ('9USD', {('USD', '9')}), ('售价CAD 89。', {('CAD', '89')}),
    ('CNY 4,999.', {('CNY', '4999')}), ('价格4999元', {('CNY', '4999')}),
    ('$9.99/month', {('USD', '9.99')}), ('EUR29', {('EUR', '29')}),
    ('9 USD 和人民币10元', {('USD', '9'), ('CNY', '10')}),
])
def test_actual_price_forms_remain_detected(text, expected):
    assert RecoveryMixin._price_claims(text) == expected


def test_real_document_hash_answer_completes_without_price_recovery(tmp_path):
    _, _, store, runtime = runtime_for(tmp_path)
    answer = '末段3105原文MARKER_0e7926ef545f081a，文件SHA256为1ea072356b5a2323e960e83c5c21c3a516ca91cf5cce2742226f89cadbc064ee。'
    try:
        result = Runner(FakeModel(Response(content=answer)), runtime).start('解释测试文档的段落与哈希。')
        assert result['status'] == 'completed' and result['answer'] == answer
        assert not store.record.run_runtime['recovery_counts'].get('evidence_claim')
        runner = Runner(FakeModel(), runtime)
        assert runner._unsupported_price_claims('价格 USD 89')
    finally:
        runtime.close()
