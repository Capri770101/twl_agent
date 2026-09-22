from agent.agent import _entity_query_ok
from agent.engine.ui_protocol import ToolCallRecord


def test_entity_query_evidence_ignores_system_records():
    system = ToolCallRecord(name='platform_db_query_entity', arguments={'entity': 'plan'}, source='system')
    model = ToolCallRecord(name='platform_db_query_entity', arguments={'entity': 'plan'}, source='model')
    assert not _entity_query_ok([system], 'plan')
    assert _entity_query_ok([system, model], 'plan')
