import json
from agent.knowledge_answer import knowledge_fallback_reply
from agent.engine.ui_protocol import ToolCallRecord


def test_public_knowledge_steps_are_used_not_placeholder():
    record = ToolCallRecord(name='retrieve_knowledge', result=json.dumps({'results':[{'tips':['洗净花瓶', '水浑浊时换水'], 'private':'hidden'}]}))
    answer = knowledge_fallback_reply([record])
    assert '洗净花瓶' in answer and 'hidden' not in answer
    assert '查看下方卡片' not in answer


def test_bad_or_failed_results_do_not_leak():
    for result in ('not json', '[]', '{"results":null}'):
        assert '暂时没有' in knowledge_fallback_reply([ToolCallRecord(name='retrieve_knowledge',result=result,status='error')])
